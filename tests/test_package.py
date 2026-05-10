"""Tests for CamFlow v1.2 P0 packaged workflows.

Cover:
- Slug primitives + manifest/lock validation
- Tree digest determinism
- create from a finished tool-only run + round-trip validate
- Path traversal / symlink / hardlink rejection
- Install / list / uninstall lifecycle, digest collision rejection
- run --package executes without Planner; package metadata in trace
- Status surfaces package info
- Replan from a packaged run records parent_package
- run.tool nodes fail package create (P0 limitation)
- Missing skill in archive fails validate
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runner import package as pkg  # noqa: E402
from runner.runtime import (  # noqa: E402
    Workflow,
    run_workflow,
    _execute_with_optional_auto_replan,
    _summarize_status,
    _render_status_human,
    _cmd_run,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def _envelope_tool(value=1) -> str:
    return f"""
cat <<EOF
{{"status":"success","data":{{"value":{value}}},"error":null,"feedback":null,"request_human":false}}
EOF
"""


def _make_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -e\n" + body)
    path.chmod(0o755)


def _stage_successful_run(tmp_path: Path, *,
                          shipped_skill_md: str = "# Skill: analyzer\nstub")  -> Path:
    """Build a project with a tiny single-skill workflow that has
    already completed successfully. Returns the run dir."""
    proj = tmp_path / "proj"
    skills_dir = proj / "skills" / "analyzer"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(shipped_skill_md)

    rd = proj / ".camflow" / "run"
    rd.mkdir(parents=True)
    spec = {
        "workflow": "tiny", "version": "1.1",
        "goal": "Demonstrate a packaged workflow.",
        "nodes": [{
            "id": "step", "goal": "do it", "steps": ["s"],
            "run": {"skill": "analyzer"},
            "output_schema": {"x": "integer"},
            "verify": {"command": "true"},
        }],
    }
    (rd / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (rd / "prompt.txt").write_text("p")
    # Trace ends with a workflow_completed success — the create gate.
    events = [
        {"step": 1, "ts": "t", "event": "workflow_started"},
        {"step": 2, "ts": "t", "event": "node_started",
         "node": "step", "attempt": 1},
        {"step": 3, "ts": "t", "event": "node_completed",
         "node": "step", "attempt": 1, "status": "success"},
        {"step": 4, "ts": "t", "event": "workflow_completed",
         "status": "success"},
    ]
    (rd / "trace.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    # rev 1 recording.
    rev1 = rd / "dag_revisions" / "0001"
    rev1.mkdir(parents=True)
    (rev1 / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (rev1 / "manifest.json").write_text(json.dumps(
        {"revision": 1, "parent_revision": None,
         "reason": "initial_plan", "workflow_goal": spec["goal"]},
        indent=2))
    return rd


# ─── Slug + digest primitives ────────────────────────────────────────


class TestPackageDigest:
    def test_canonical_tree_digest_is_stable(self):
        files = {
            "manifest.yaml": "sha256:aaa",
            "workflow.yaml": "sha256:bbb",
            "skills/x/SKILL.md": "sha256:ccc",
        }
        d1 = pkg.canonical_tree_digest(files)
        d2 = pkg.canonical_tree_digest(dict(reversed(list(files.items()))))
        assert d1 == d2  # input ordering doesn't matter
        assert d1.startswith("sha256:")

    def test_canonical_tree_digest_changes_on_path_or_content(self):
        base = {"a": "sha256:1", "b": "sha256:2"}
        a = pkg.canonical_tree_digest(base)
        b = pkg.canonical_tree_digest({"a": "sha256:1", "c": "sha256:2"})
        c = pkg.canonical_tree_digest({"a": "sha256:1", "b": "sha256:9"})
        assert a != b and a != c

    def test_parse_package_id(self):
        assert pkg.parse_package_id("foo@1.0.0") == ("foo", "1.0.0")
        with pytest.raises(pkg.PackageError):
            pkg.parse_package_id("foo")
        with pytest.raises(pkg.PackageError):
            pkg.parse_package_id("FOO@1.0.0")
        with pytest.raises(pkg.PackageError):
            pkg.parse_package_id("foo@bogus")


# ─── Manifest / lock validation ──────────────────────────────────────


class TestManifestValidation:
    def _good_manifest(self) -> dict:
        return {
            "package_schema": "1",
            "name": "tiny",
            "version": "0.1.0",
            "workflow_spec": "1.1",
            "workflow_entry": "workflow.yaml",
            "runtime": {"min_camflow": "1.2.0"},
            "skills": {"analyzer": {"path": "skills/analyzer/SKILL.md"}},
            "provenance": {},
        }

    def test_good_manifest_validates(self):
        assert pkg._validate_manifest(self._good_manifest()) == []

    def test_unknown_field_fails(self):
        m = self._good_manifest()
        m["mystery_field"] = "?"
        errs = pkg._validate_manifest(m)
        assert any("unknown top-level key" in e for e in errs)

    def test_missing_required_fails(self):
        m = self._good_manifest()
        del m["skills"]
        errs = pkg._validate_manifest(m)
        assert any("missing required key" in e for e in errs)

    def test_planner_required_must_be_false(self):
        m = self._good_manifest()
        m["runtime"]["planner_required_for_initial_run"] = True
        errs = pkg._validate_manifest(m)
        assert any("planner_required_for_initial_run" in e for e in errs)

    def test_skill_path_must_match(self):
        m = self._good_manifest()
        m["skills"]["analyzer"]["path"] = "skills/wrong.md"
        errs = pkg._validate_manifest(m)
        assert any("skills.analyzer.path" in e for e in errs)


# ─── Create / round-trip ─────────────────────────────────────────────


class TestPackageCreate:
    def test_create_round_trips(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        out = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=out, description="demo")
        assert out.is_file()
        # Validate the freshly built archive cleanly.
        assert pkg.validate_package(out) == []
        info = pkg.inspect_package(out)
        assert info["name"] == "tiny"
        assert info["version"] == "0.1.0"
        assert info["skills"] == ["analyzer"]
        assert info["content_digest"].startswith("sha256:")

    def test_create_fails_on_run_tool_node(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        # Mutate the workflow.yaml to add a tool node — package create
        # should refuse this (P0 limitation).
        spec = yaml.safe_load((rd / "workflow.yaml").read_text())
        spec["nodes"].append({
            "id": "audit", "goal": "g", "steps": ["s"],
            "run": {"tool": "scripts/run_tests.sh"},
            "output_schema": {"passed": "boolean"},
            "verify": {"command": "true"},
        })
        (rd / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        with pytest.raises(pkg.PackageError, match="run.tool"):
            pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                                out=tmp_path / "x.camflowpkg")

    def test_create_fails_on_halted_run(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        # Replace the trace's last event with a halted one.
        (rd / "trace.jsonl").write_text(json.dumps(
            {"step": 1, "ts": "t", "event": "workflow_halted",
             "node": "step", "reason": "x"}) + "\n")
        (rd / "halt.json").write_text(json.dumps(
            {"halted_node": "step", "kind": "halt", "reason": "x"}))
        with pytest.raises(pkg.PackageError, match="halted|workflow_completed"):
            pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                                out=tmp_path / "x.camflowpkg")

    def test_create_fails_on_missing_skill(self, tmp_path, monkeypatch):
        rd = _stage_successful_run(tmp_path)
        # Drop the skill — both project and repo lookup should fail.
        proj = rd.parent.parent
        skill_path = proj / "skills" / "analyzer" / "SKILL.md"
        skill_path.unlink()
        # Point repo_root at a tempdir without a skills dir so the
        # repo fallback can't satisfy.
        with pytest.raises(pkg.PackageError, match="not found"):
            pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                                out=tmp_path / "x.camflowpkg",
                                repo_root=tmp_path)

    def test_create_fails_on_invalid_workflow_yaml(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        spec = yaml.safe_load((rd / "workflow.yaml").read_text())
        del spec["nodes"][0]["goal"]
        (rd / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))

        with pytest.raises(pkg.PackageError, match="workflow.yaml is invalid"):
            pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                                out=tmp_path / "x.camflowpkg")


# ─── Archive security ────────────────────────────────────────────────


class TestArchiveSecurity:
    def _build_evil_archive(self, out: Path,
                            extra_member_setup) -> None:
        """Build a tar.gz with a malicious extra member."""
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w",
                          format=tarfile.USTAR_FORMAT) as t:
            # Root dir.
            ti = tarfile.TarInfo(name="camflowpkg")
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            t.addfile(ti)
            # Add the user-supplied evil entry.
            extra_member_setup(t)
        with open(out, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(tar_buf.getvalue())

    def test_path_traversal_rejected(self, tmp_path):
        out = tmp_path / "bad.camflowpkg"

        def evil(t):
            ti = tarfile.TarInfo(name="camflowpkg/../escape.txt")
            ti.type = tarfile.REGTYPE
            ti.size = 4
            t.addfile(ti, io.BytesIO(b"hi!\n"))

        self._build_evil_archive(out, evil)
        errors = pkg.validate_package(out)
        assert any(".." in e or "not allowed" in e for e in errors), errors

    def test_absolute_path_rejected(self, tmp_path):
        out = tmp_path / "bad.camflowpkg"

        def evil(t):
            ti = tarfile.TarInfo(name="/etc/passwd")
            ti.type = tarfile.REGTYPE
            ti.size = 0
            t.addfile(ti)

        self._build_evil_archive(out, evil)
        errors = pkg.validate_package(out)
        assert any("absolute path" in e or "not allowed" in e
                   for e in errors), errors

    def test_symlink_rejected(self, tmp_path):
        out = tmp_path / "bad.camflowpkg"

        def evil(t):
            ti = tarfile.TarInfo(name="camflowpkg/link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            t.addfile(ti)

        self._build_evil_archive(out, evil)
        errors = pkg.validate_package(out)
        assert any("symlink" in e or "hardlink" in e for e in errors), errors

    def test_hardlink_rejected(self, tmp_path):
        out = tmp_path / "bad.camflowpkg"

        def evil(t):
            ti = tarfile.TarInfo(name="camflowpkg/hardlink")
            ti.type = tarfile.LNKTYPE
            ti.linkname = "camflowpkg/manifest.yaml"
            t.addfile(ti)

        self._build_evil_archive(out, evil)
        errors = pkg.validate_package(out)
        assert any("symlink" in e or "hardlink" in e for e in errors), errors

    def test_unknown_path_family_rejected(self, tmp_path):
        out = tmp_path / "bad.camflowpkg"

        def evil(t):
            ti = tarfile.TarInfo(name="camflowpkg/.ssh/authorized_keys")
            ti.type = tarfile.REGTYPE
            ti.size = 0
            t.addfile(ti)

        self._build_evil_archive(out, evil)
        errors = pkg.validate_package(out)
        assert any("not in allowed family" in e or "not allowed" in e
                   for e in errors), errors


# ─── Validate-on-archive: missing skill, digest mismatch ─────────────


class TestValidateBundle:
    def test_missing_skill_in_archive_fails(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        out = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=out)
        # Re-pack without the skill file.
        files = pkg._read_package_files(out)
        del files["skills/analyzer/SKILL.md"]
        # Recompute lock so digest is internally consistent (then the
        # workflow-skill cross-check is what fails).
        digest_map = {p: pkg._file_sha256(b)
                      for p, b in files.items() if p != "lock.json"}
        lock = json.loads(files["lock.json"].decode("utf-8"))
        lock["files"] = digest_map
        lock["content_digest"] = pkg.canonical_tree_digest(digest_map)
        files["lock.json"] = (json.dumps(lock, indent=2, sort_keys=True)
                              + "\n").encode("utf-8")

        out2 = tmp_path / "broken.camflowpkg"
        pkg._write_archive(out2, files)
        errors = pkg.validate_package(out2)
        assert any("skill" in e.lower() for e in errors), errors

    def test_invalid_workflow_yaml_in_archive_fails(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        out = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=out)

        files = pkg._read_package_files(out)
        spec = yaml.safe_load(files["workflow.yaml"].decode("utf-8"))
        del spec["nodes"][0]["goal"]
        files["workflow.yaml"] = yaml.safe_dump(
            spec, sort_keys=False).encode("utf-8")
        digest_map = {p: pkg._file_sha256(b)
                      for p, b in files.items() if p != "lock.json"}
        lock = json.loads(files["lock.json"].decode("utf-8"))
        lock["files"] = digest_map
        lock["content_digest"] = pkg.canonical_tree_digest(digest_map)
        files["lock.json"] = (json.dumps(lock, indent=2, sort_keys=True)
                              + "\n").encode("utf-8")

        out2 = tmp_path / "broken-workflow.camflowpkg"
        pkg._write_archive(out2, files)
        errors = pkg.validate_package(out2)
        assert any("workflow.yaml validation failed" in e
                   for e in errors), errors


# ─── Install lifecycle ───────────────────────────────────────────────


class TestInstallLifecycle:
    def test_install_user_scope_round_trips(self, tmp_path, monkeypatch):
        # Re-home so the install lands inside the test tmp, not the
        # real user homedir.
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        # Force pathlib.Path.home() to honor $HOME.
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))

        rd = _stage_successful_run(tmp_path)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=archive)
        target = pkg.install_package(archive)
        assert target.is_dir()
        assert (target / "camflowpkg" / "manifest.yaml").is_file()
        assert (target / "installed.json").is_file()

        # list_installed surfaces it.
        items = pkg.list_installed()
        assert any(m["name"] == "tiny" and m["version"] == "0.1.0"
                   for m in items)

        # resolve_installed finds the camflowpkg dir.
        camflowpkg = pkg.resolve_installed("tiny", "0.1.0")
        assert camflowpkg.is_dir()
        assert camflowpkg.name == "camflowpkg"

        # uninstall removes it; list returns to empty.
        assert pkg.uninstall_package("tiny", "0.1.0") is True
        assert pkg.list_installed() == []
        assert pkg.uninstall_package("tiny", "0.1.0") is False

    def test_install_project_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))
        rd = _stage_successful_run(tmp_path)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=archive)
        proj = tmp_path / "myproj"
        proj.mkdir()
        target = pkg.install_package(archive, project_local=True,
                                      project_root=proj)
        assert (proj / ".camflow" / "packages" / "tiny" / "0.1.0"
                / "camflowpkg" / "manifest.yaml").is_file()
        # Resolver: project-local wins over user.
        cf = pkg.resolve_installed("tiny", "0.1.0", project_root=proj)
        assert str(cf).startswith(str(proj))

    def test_install_digest_collision_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))
        rd1 = _stage_successful_run(tmp_path,
                                    shipped_skill_md="# Skill\nfirst\n")
        a1 = tmp_path / "a.camflowpkg"
        pkg.create_package(run_dir=rd1, name="tiny", version="0.1.0",
                            out=a1)
        # First install OK.
        pkg.install_package(a1)

        # Build a different package with same name@version (different skill).
        # _stage_successful_run reuses the same rd path so we point a
        # second project at a fresh tmp subdir.
        proj2 = tmp_path / "p2"
        proj2.mkdir()
        rd2 = _stage_successful_run(proj2,
                                    shipped_skill_md="# Skill\ndifferent\n")
        a2 = tmp_path / "b.camflowpkg"
        pkg.create_package(run_dir=rd2, name="tiny", version="0.1.0",
                            out=a2)
        # Different digest → should refuse to silently replace.
        with pytest.raises(pkg.PackageError, match="DIFFERENT content_digest"):
            pkg.install_package(a2)
        # Cleanup so other tests get a fresh home.
        pkg.uninstall_package("tiny", "0.1.0")


# ─── camflow run --package end-to-end ────────────────────────────────


class TestPackageRun:
    def _project_with_envelope_tool_skill(self, tmp_path: Path,
                                          skill_value: int = 1) -> Path:
        """A project whose skills/<name>/SKILL.md is a tiny shell that
        outputs an envelope. Skill execution goes via exec_skill →
        camc.run_and_collect; we'll monkey-patch that for the test."""
        proj = tmp_path / "proj"
        skills_dir = proj / "skills" / "analyzer"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            f"# Skill: analyzer (yields value={skill_value})\n")
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        spec = {
            "workflow": "tiny", "version": "1.1",
            "goal": "g.",
            "nodes": [{
                "id": "step", "goal": "g", "steps": ["s"],
                "run": {"skill": "analyzer"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
            }],
        }
        (rd / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (rd / "prompt.txt").write_text("p")
        events = [
            {"step": 1, "ts": "t", "event": "workflow_started"},
            {"step": 2, "ts": "t", "event": "node_started",
             "node": "step", "attempt": 1},
            {"step": 3, "ts": "t", "event": "node_completed",
             "node": "step", "attempt": 1, "status": "success"},
            {"step": 4, "ts": "t", "event": "workflow_completed",
             "status": "success"},
        ]
        (rd / "trace.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n")
        rev1 = rd / "dag_revisions" / "0001"
        rev1.mkdir(parents=True)
        (rev1 / "workflow.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (rev1 / "manifest.json").write_text(json.dumps(
            {"revision": 1, "parent_revision": None,
             "reason": "initial_plan", "workflow_goal": spec["goal"]}))
        return rd

    def test_run_package_skips_planner_and_executes(
            self, tmp_path, monkeypatch):
        from runner import runtime as rt

        # Set up a project + build a package from it.
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))

        rd_src = self._project_with_envelope_tool_skill(tmp_path,
                                                         skill_value=42)
        spec = yaml.safe_load((rd_src / "workflow.yaml").read_text())
        spec["nodes"][0]["verify"] = {
            "command": (
                "python3 -c 'import os,pathlib,sys; "
                "sys.exit(0 if pathlib.Path(os.environ[\"CAMFLOW_PROJECT_ROOT\"]).name "
                "== \"camflowpkg\" else 1)'"
            )
        }
        (rd_src / "workflow.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=False))
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd_src, name="tiny", version="0.1.0",
                            out=archive,
                            description="tiny demo package")
        target = pkg.install_package(archive)

        # Stub camc so the skill node returns success without an LLM.
        camc_calls: list[dict] = []

        def fake_camc(*, prompt, workspace, name, tag, output_file,
                      timeout_s, write_id_to=None):  # noqa: ARG001
            camc_calls.append({"name": name, "tag": tag,
                                "skill_md": prompt[:60]})
            envelope = {
                "status": "success", "data": {"value": 99},
                "error": None, "feedback": None, "request_human": False,
            }
            (Path(workspace) / output_file).write_text(json.dumps(envelope))
            return ("aid", envelope)

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_camc)

        # Run the package via the same code path the CLI uses.
        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("tiny@0.1.0", run_dir, run_proj)
        assert rc == 0

        # Trace's first event must be workflow_started with package={...},
        # planner_invoked=False — and the run dir must NOT have a
        # planner/ sub-dir (Planner was never invoked).
        events = [json.loads(line) for line in
                  (run_dir / "trace.jsonl").read_text().splitlines()
                  if line.strip()]
        first = events[0]
        assert first["event"] == "workflow_started"
        assert first["planner_invoked"] is False
        assert first["package"]["name"] == "tiny"
        assert first["package"]["version"] == "0.1.0"
        assert first["package"]["content_digest"].startswith("sha256:")
        assert not (run_dir / "planner").exists()

        # package.json materialized.
        pkg_meta = json.loads((run_dir / "package.json").read_text())
        assert pkg_meta["name"] == "tiny"
        assert pkg_meta["content_digest"].startswith("sha256:")

        # Skill was sourced from the package, not the source-tree project.
        assert camc_calls, "exec_skill never called"
        assert "yields value=42" in camc_calls[0]["skill_md"]

        # Cleanup install so other tests' home stays clean.
        pkg.uninstall_package("tiny", "0.1.0")

    def test_package_root_missing_skill_fails_without_host_fallback(
            self, tmp_path):
        spec = {
            "workflow": "tiny", "version": "1.1",
            "nodes": [{
                "id": "step", "goal": "g", "steps": ["s"],
                "run": {"skill": "analyzer"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
            }],
        }
        package_root = tmp_path / "camflowpkg"
        package_root.mkdir()
        wf = Workflow(spec, tmp_path / "run",
                      project_root=package_root,
                      package_root=package_root)
        env = wf.nodes_by_id["step"].run(
            wf, {"dag_revision": 1}, tmp_path / "attempt", 1)
        assert env["status"] == "fail"
        assert env["error"]["code"] == "PACKAGE_SKILL_NOT_FOUND"

    def test_run_package_missing_install_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))
        from runner import runtime as rt
        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("nope@1.0.0", run_dir, run_proj)
        assert rc == 1


# ─── Status surfacing ────────────────────────────────────────────────


class TestStatusPackage:
    def test_status_shows_package_metadata(self, tmp_path):
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "workflow.yaml").write_text(yaml.safe_dump({
            "workflow": "x", "version": "1.1",
            "nodes": [{"id": "n", "goal": "g", "steps": ["s"],
                        "run": {"skill": "analyzer"}}],
        }, sort_keys=False))
        (rd / "package.json").write_text(json.dumps({
            "name": "tiny", "version": "0.1.0",
            "content_digest": "sha256:cafe",
        }))
        s = _summarize_status(rd)
        assert s["package"]["name"] == "tiny"
        text = _render_status_human(s)
        assert "package: tiny@0.1.0" in text
        assert "sha256:cafe" in text


# ─── Replan from packaged run records parent_package ─────────────────


class TestReplanFromPackage:
    def _stub_planner_returning(self, monkeypatch, new_yaml: str):
        from runner import runtime as rt
        orig_init = rt.Workflow.__init__

        class _StubWorkflow:
            def __init__(self, spec, run_dir, *, project_root=None,
                         resume=False, replan=False, package_root=None):
                self.spec = spec
                self.run_dir = Path(run_dir)
                self.run_dir.mkdir(parents=True, exist_ok=True)
                self.run_id = "stub"
                self.dag_revision = 1
                self._is_user_workflow = False
                self.nodes_by_id = {
                    "render_yaml": type("N", (), {
                        "id": "render_yaml",
                        "output": {"status": "success",
                                   "data": {"yaml_text": new_yaml}},
                    })()
                }

            def trace(self, *a, **k):  # noqa: ARG002
                pass

            def execute_dag(self, *, max_attempts=None):  # noqa: ARG002
                return "done"

            def cleanup(self):
                pass

        def patched_init(self, spec, run_dir, *, resume=False,
                         replan=False, project_root=None,
                         package_root=None):
            rd = Path(run_dir).resolve()
            if "planner-rev" in rd.name:
                stub = _StubWorkflow(spec, rd, project_root=project_root,
                                     resume=resume, replan=replan,
                                     package_root=package_root)
                self.__class__ = _StubWorkflow
                self.__dict__.update(stub.__dict__)
                return
            orig_init(self, spec, rd, resume=resume, replan=replan,
                      project_root=project_root,
                      package_root=package_root)

        monkeypatch.setattr(rt.Workflow, "__init__", patched_init)

    def test_replan_records_parent_package(self, tmp_path, monkeypatch):
        from runner import runtime as rt
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        _make_executable(scripts / "ok.sh", _envelope_tool(value=1))
        _make_executable(scripts / "bad.sh", _envelope_tool(value=0))
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("p")
        initial = {
            "workflow": "rev1", "version": "1.1",
            "goal": "g.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/bad.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "false"},
                "retry": 1,
            }],
        }
        replan_yaml = yaml.safe_dump({
            "workflow": "rev2", "version": "1.1",
            "goal": "g.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/ok.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
                "retry": 1,
            }],
        }, sort_keys=False)
        self._stub_planner_returning(monkeypatch, replan_yaml)

        package_meta = {
            "name": "tiny", "version": "0.1.0",
            "content_digest": "sha256:dead",
        }
        result = _execute_with_optional_auto_replan(
            initial, rd, package_meta=package_meta)
        assert result == "done"

        rev2_manifest = json.loads(
            (rd / "dag_revisions" / "0002" / "manifest.json").read_text())
        assert rev2_manifest["parent_package"] == package_meta
