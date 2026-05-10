"""Tests for CamFlow v1.2 P0 packaged workflows.

Cover:
- Slug primitives + manifest/lock validation
- Tree digest determinism
- create from a finished skill run + round-trip validate
- Path traversal / symlink / hardlink rejection
- Install / list / uninstall lifecycle, digest collision rejection
- run --package executes without Planner; package metadata in trace
- Status surfaces package info
- Replan from a packaged run records parent_package
- run.tool nodes fail package create
- Missing skill in archive fails validate
"""
from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
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
        # verify.command checks the new materialization contract:
        # CAMFLOW_PROJECT_ROOT must point at the run dir (not the
        # installed package) AND the skill must have been materialized
        # there as <run>/skills/analyzer/SKILL.md.
        spec = yaml.safe_load((rd_src / "workflow.yaml").read_text())
        spec["nodes"][0]["verify"] = {
            "command": (
                "python3 -c 'import os,pathlib,sys; "
                "root=pathlib.Path(os.environ[\"CAMFLOW_PROJECT_ROOT\"]); "
                "sys.exit(0 if (root.name == \"run\" and "
                "(root/\"skills\"/\"analyzer\"/\"SKILL.md\").is_file()) "
                "else 1)'"
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

        # Skill was sourced from the package — and materialized into
        # the run dir, NOT looked up from the installed package on
        # each attempt.
        assert camc_calls, "exec_skill never called"
        assert "yields value=42" in camc_calls[0]["skill_md"]
        assert (run_dir / "skills" / "analyzer" / "SKILL.md").is_file()
        assert (run_dir / "workflow.yaml").is_file()

        # Cleanup install so other tests' home stays clean.
        pkg.uninstall_package("tiny", "0.1.0")

    def test_regression_run_package_skill_verify_command_e2e(
            self, tmp_path, monkeypatch):
        """End-to-end regression for the active v1.2 contract.

        A proven skill-only workflow is packaged, installed, then run via
        `camflow run --package`. The replay must skip Planner, execute the
        same `run.skill` DAG, and keep deterministic validation in
        `verify.command`.
        """
        from runner import runtime as rt

        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))

        source_proj = tmp_path / "source"
        (source_proj / "skills" / "analyzer").mkdir(parents=True)
        (source_proj / "skills" / "analyzer" / "SKILL.md").write_text(
            "# Skill: analyzer\n")
        source_run = source_proj / ".camflow" / "run"
        spec = {
            "workflow": "skill_verify_e2e",
            "version": "1.1",
            "goal": "Produce a number and summarize it deterministically.",
            "nodes": [
                {
                    "id": "produce",
                    "goal": "Produce the deterministic input.",
                    "steps": ["emit n=7"],
                    "run": {"skill": "analyzer"},
                    "output_schema": {"n": "integer"},
                    "verify": {"command": "true"},
                },
                {
                    "id": "summarize",
                    "goal": "Summarize the produced value.",
                    "needs": ["produce"],
                    "steps": ["read upstream.produce", "emit summary"],
                    "run": {"skill": "analyzer"},
                    "output_schema": {"summary": "string"},
                    "verify": {
                        "command": (
                            "python3 -c 'import json,sys; "
                            "env=json.load(open(\"agent_output.json\")); "
                            "sys.exit(0 if "
                            "env.get(\"data\", {}).get(\"summary\") == "
                            "\"ok:7\" else 1)'"
                        )
                    },
                },
            ],
        }

        def fake_exec_skill(skill_md, node, input_dict, workspace,
                            attempt_n, run_id_tag, **kwargs):  # noqa: ARG001
            if node.id == "produce":
                return {
                    "status": "success",
                    "data": {"n": 7},
                    "error": None,
                    "feedback": None,
                    "request_human": False,
                }
            n = input_dict["upstream"]["produce"]["data"]["n"]
            return {
                "status": "success",
                "data": {"summary": f"ok:{n}"},
                "error": None,
                "feedback": None,
                "request_human": False,
            }

        monkeypatch.setattr(rt, "exec_skill", fake_exec_skill)
        assert rt.run_workflow(spec, source_run) == "done"
        source_summary = json.loads(
            (source_run / "nodes" / "summarize" / "attempt-1"
             / "output.json").read_text())

        archive = tmp_path / "skillverify.camflowpkg"
        pkg.create_package(run_dir=source_run, name="skillverify",
                           version="0.1.0", out=archive,
                           description="skill + verify.command e2e")
        pkg.install_package(archive)

        replay_proj = tmp_path / "replay"
        replay_proj.mkdir()
        monkeypatch.chdir(replay_proj)
        rc = _cmd_run(["--package", "skillverify@0.1.0"])
        assert rc == 0

        replay_run = replay_proj / ".camflow" / "run"
        replay_summary = json.loads(
            (replay_run / "nodes" / "summarize" / "attempt-1"
             / "output.json").read_text())
        assert replay_summary["data"] == source_summary["data"]
        assert (replay_run / "workflow.yaml").is_file()
        assert (replay_run / "skills" / "analyzer" / "SKILL.md").is_file()
        assert not (replay_run / "planner").exists()
        assert json.loads(
            (replay_run / "package.json").read_text())["name"] == "skillverify"

        pkg.uninstall_package("skillverify", "0.1.0")

    def test_regression_package_replay_recreates_venv_and_outputs(
            self, tmp_path, monkeypatch):
        """Heavier replay E2E: skill-run command work is reproduced.

        The fake skill creates a venv, installs a local wheel with pip,
        writes an intermediate marker, then a downstream node runs the
        venv Python to generate the final result. Package replay must
        recreate those run-local artifacts and produce the same final
        data without invoking Planner.
        """
        from runner import runtime as rt

        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))

        source_proj = tmp_path / "venv-source"
        (source_proj / "skills" / "builder").mkdir(parents=True)
        (source_proj / "skills" / "builder" / "SKILL.md").write_text(
            "# Skill: builder\nRuns local command work for replay tests.\n")
        source_run = source_proj / ".camflow" / "run"
        spec = {
            "workflow": "venv_replay_demo",
            "version": "1.1",
            "goal": "Build a run-local Python environment and result.",
            "nodes": [
                {
                    "id": "prepare_env",
                    "goal": "Create a venv and install the local wheel.",
                    "steps": ["create venv", "pip install local wheel"],
                    "run": {"skill": "builder"},
                    "output_schema": {
                        "python": "string",
                        "installed": "boolean",
                    },
                    "verify": {
                        "command": (
                            "python3 -c 'import json,pathlib,sys; "
                            "d=json.load(open(\"agent_output.json\"))[\"data\"]; "
                            "sys.exit(0 if d[\"installed\"] and "
                            "pathlib.Path(d[\"python\"]).is_file() else 1)'"
                        )
                    },
                },
                {
                    "id": "make_result",
                    "goal": "Run the installed package and write result files.",
                    "needs": ["prepare_env"],
                    "steps": ["run venv python", "write result"],
                    "run": {"skill": "builder"},
                    "output_schema": {
                        "result": "string",
                        "summary": "string",
                    },
                    "verify": {
                        "command": (
                            "python3 -c 'import json,sys; "
                            "d=json.load(open(\"agent_output.json\"))[\"data\"]; "
                            "sys.exit(0 if d[\"result\"] == \"42\" and "
                            "d[\"summary\"] == \"answer=42\" else 1)'"
                        )
                    },
                },
            ],
        }

        def run_dir_from_workspace(workspace: Path) -> Path:
            p = workspace.resolve()
            while p.name != "run" and p.parent != p:
                p = p.parent
            return p

        def build_local_wheel(wheelhouse: Path) -> Path:
            wheelhouse.mkdir(parents=True, exist_ok=True)
            wheel = wheelhouse / "demo_add-0.1-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr(
                    "demo_add/__init__.py",
                    "def add(a, b):\n    return a + b\n")
                zf.writestr(
                    "demo_add-0.1.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: demo-add\nVersion: 0.1\n")
                zf.writestr(
                    "demo_add-0.1.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nGenerator: camflow-test\n"
                    "Root-Is-Purelib: true\nTag: py3-none-any\n")
                zf.writestr("demo_add-0.1.dist-info/RECORD", "")
            return wheel

        def fake_exec_skill(skill_md, node, input_dict, workspace,
                            attempt_n, run_id_tag, **kwargs):  # noqa: ARG001
            assert "Skill: builder" in skill_md
            workspace = Path(workspace)
            run_dir = run_dir_from_workspace(workspace)
            artifact_root = run_dir / "artifacts"
            if node.id == "prepare_env":
                wheelhouse = artifact_root / "wheelhouse"
                build_local_wheel(wheelhouse)
                venv = run_dir / "venv"
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv)],
                    check=True, text=True, capture_output=True)
                py = venv / "bin" / "python"
                subprocess.run(
                    [str(py), "-m", "pip", "install", "--no-index",
                     "--find-links", str(wheelhouse), "demo-add==0.1"],
                    check=True, text=True, capture_output=True)
                marker = artifact_root / "prepare_env.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({
                    "python": str(py),
                    "installed": True,
                }))
                return {
                    "status": "success",
                    "data": {"python": str(py), "installed": True},
                    "error": None,
                    "feedback": None,
                    "request_human": False,
                }

            py = input_dict["upstream"]["prepare_env"]["data"]["python"]
            cp = subprocess.run(
                [py, "-c", "import demo_add; print(demo_add.add(20, 22))"],
                check=True, text=True, capture_output=True)
            result = cp.stdout.strip()
            result_dir = artifact_root / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "answer.txt").write_text(result)
            (result_dir / "summary.json").write_text(json.dumps({
                "summary": f"answer={result}",
            }))
            return {
                "status": "success",
                "data": {"result": result, "summary": f"answer={result}"},
                "error": None,
                "feedback": None,
                "request_human": False,
            }

        monkeypatch.setattr(rt, "exec_skill", fake_exec_skill)
        assert rt.run_workflow(spec, source_run) == "done"
        source_final = json.loads(
            (source_run / "nodes" / "make_result" / "attempt-1"
             / "output.json").read_text())["data"]

        archive = tmp_path / "venvreplay.camflowpkg"
        pkg.create_package(run_dir=source_run, name="venvreplay",
                           version="0.1.0", out=archive,
                           description="venv replay e2e")
        pkg.install_package(archive)

        replay_proj = tmp_path / "venv-replay"
        replay_proj.mkdir()
        monkeypatch.chdir(replay_proj)
        rc = _cmd_run(["--package", "venvreplay@0.1.0"])
        assert rc == 0
        replay_run = replay_proj / ".camflow" / "run"
        replay_final = json.loads(
            (replay_run / "nodes" / "make_result" / "attempt-1"
             / "output.json").read_text())["data"]

        assert replay_final == source_final == {
            "result": "42",
            "summary": "answer=42",
        }
        assert (replay_run / "venv" / "bin" / "python").is_file()
        assert (replay_run / "artifacts" / "prepare_env.json").is_file()
        assert (replay_run / "artifacts" / "results" / "answer.txt"
                ).read_text() == "42"
        assert (replay_run / "skills" / "builder" / "SKILL.md").is_file()
        assert not (replay_run / "planner").exists()

        pkg.uninstall_package("venvreplay", "0.1.0")

    def test_run_package_survives_install_dir_skill_deletion(
            self, tmp_path, monkeypatch):
        """Once the package run has materialized skills/workflow into
        `<run>/`, normal node execution should not depend on the
        installed package directory anymore.

        Concretely: install a package, run --package once so the run
        dir is fully materialized, delete the installed package's
        SKILL.md, kick off node execution against the SAME run dir,
        and confirm the skill still loads from `<run>/skills/`."""
        from runner import runtime as rt

        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls: Path(os.environ["HOME"])))

        rd_src = self._project_with_envelope_tool_skill(tmp_path,
                                                         skill_value=7)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd_src, name="tiny", version="0.1.0",
                            out=archive)
        install_dir = pkg.install_package(archive)

        skill_calls: list[str] = []

        def fake_camc(*, prompt, workspace, name, tag, output_file,
                      timeout_s, write_id_to=None):  # noqa: ARG001
            skill_calls.append(prompt[:60])
            envelope = {
                "status": "success", "data": {"value": 1},
                "error": None, "feedback": None, "request_human": False,
            }
            (Path(workspace) / output_file).write_text(json.dumps(envelope))
            return ("aid", envelope)

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_camc)

        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("tiny@0.1.0", run_dir, run_proj)
        assert rc == 0
        materialized_skill = (run_dir / "skills" / "analyzer" / "SKILL.md")
        assert materialized_skill.is_file()
        first_round_text = materialized_skill.read_text()
        assert "yields value=7" in first_round_text

        # Delete the installed package's skill — the run dir's copy
        # should still be enough to drive node execution. We reach
        # into Node.run directly via Workflow constructed against the
        # materialized run dir.
        installed_skill = (install_dir / "camflowpkg" / "skills"
                           / "analyzer" / "SKILL.md")
        assert installed_skill.is_file()
        installed_skill.unlink()

        from runner.runtime import Workflow
        spec = yaml.safe_load((run_dir / "workflow.yaml").read_text())
        wf2 = Workflow(spec, run_dir, project_root=run_dir, resume=True)
        node = wf2.nodes_by_id["step"]
        attempt_dir = run_dir / "post_unlink_attempt"
        attempt_dir.mkdir()
        env = node.run(wf2, {"dag_revision": 1}, attempt_dir, 1)
        assert env["status"] == "success"
        assert any("yields value=7" in c for c in skill_calls), skill_calls

        pkg.uninstall_package("tiny", "0.1.0")

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
                         resume=False, replan=False):
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
                         replan=False, project_root=None):
            rd = Path(run_dir).resolve()
            if "planner-rev" in rd.name:
                stub = _StubWorkflow(spec, rd, project_root=project_root,
                                     resume=resume, replan=replan)
                self.__class__ = _StubWorkflow
                self.__dict__.update(stub.__dict__)
                return
            orig_init(self, spec, rd, resume=resume, replan=replan,
                      project_root=project_root)

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
                "run": {"skill": "analyzer"},
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
                "run": {"skill": "analyzer"},
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
        monkeypatch.setattr(
            rt, "exec_skill",
            lambda *a, **k: {
                "status": "success",
                "data": {"value": 1},
                "error": None,
                "feedback": None,
                "request_human": False,
            },
        )
        result = _execute_with_optional_auto_replan(
            initial, rd, package_meta=package_meta)
        assert result == "done"

        rev2_manifest = json.loads(
            (rd / "dag_revisions" / "0002" / "manifest.json").read_text())
        assert rev2_manifest["parent_package"] == package_meta


# ───────────────────────────────────────────────────────────────────────
#  RFC §4.1 / §6 / §13 alignment — workflow_source, allow_host_skills,
#  project install bookkeeping, replan policy, package-lock + preflight
# ───────────────────────────────────────────────────────────────────────


class TestAllowHostSkillsRejected:
    """RFC §13 final paragraph — P0 must reject
    skill_resolution.allow_host_skills: true so the first package
    release stays reproducible without surprise host-skill fallback."""

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

    def test_allow_host_skills_true_is_rejected(self):
        m = self._good_manifest()
        m["skill_resolution"] = {"allow_host_skills": True}
        errs = pkg._validate_manifest(m)
        assert any("allow_host_skills" in e for e in errs), errs

    def test_allow_host_skills_false_is_accepted(self):
        m = self._good_manifest()
        m["skill_resolution"] = {"allow_host_skills": False,
                                  "external_skills": []}
        assert pkg._validate_manifest(m) == []

    def test_external_skills_nonempty_is_rejected_in_p0(self):
        m = self._good_manifest()
        m["skill_resolution"] = {
            "allow_host_skills": False,
            "external_skills": [{"name": "reviewer"}],
        }
        errs = pkg._validate_manifest(m)
        assert any("external_skills" in e for e in errs), errs

    def test_skill_resolution_omitted_is_accepted(self):
        # Default behavior should be "false / not declared", which
        # validates clean.
        m = self._good_manifest()
        assert pkg._validate_manifest(m) == []

    def test_known_keys_include_rfc_extensions(self):
        m = self._good_manifest()
        m["host_tools"] = []
        m["external_resources"] = []
        m["global_paths"] = []
        m["generated_artifacts"] = []
        m["forbidden_install_roots"] = []
        # All these new top-level keys should be accepted (RFC §6).
        assert pkg._validate_manifest(m) == []


class TestWorkflowSourceMetadata:
    """RFC §4.1 — every fresh user workflow run records workflow_source
    (type + planner_invoked + package id/digest where applicable) on
    the first workflow_started trace event AND in the status summary."""

    def _project_with_skill_workflow(self, tmp_path: Path,
                                      skill_value: int = 1) -> Path:
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

    def test_planner_run_workflow_source_in_trace(self, tmp_path, monkeypatch):
        """A non-package run_workflow call emits workflow_source.type
        == 'planner' and planner_invoked == true (the prompt-mode CLI
        always goes through Planner first)."""
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        _make_executable(scripts / "ok.sh", _envelope_tool(value=1))
        spec = {
            "workflow": "tiny", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"},
                       "output_schema": {"value": "integer"},
                       "verify": {"command": "true"}}],
        }
        from runner import runtime as rt
        monkeypatch.setattr(
            rt, "exec_skill",
            lambda *a, **k: {
                "status": "success",
                "data": {"value": 1},
                "error": None,
                "feedback": None,
                "request_human": False,
            },
        )
        rd = proj / ".camflow" / "run"
        result = run_workflow(spec, rd)
        assert result == "done"
        events = [json.loads(line) for line in
                  (rd / "trace.jsonl").read_text().splitlines() if line]
        first = events[0]
        assert first["event"] == "workflow_started"
        ws = first.get("workflow_source")
        assert ws is not None, "workflow_source missing on workflow_started"
        assert ws["type"] == "planner"
        assert ws["planner_invoked"] is True

    def test_package_run_workflow_source_in_trace(self, tmp_path,
                                                    monkeypatch):
        from runner import runtime as rt
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls:
                                          Path(os.environ["HOME"])))

        rd_src = self._project_with_skill_workflow(tmp_path,
                                                    skill_value=42)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd_src, name="tiny", version="0.1.0",
                            out=archive)
        pkg.install_package(archive)

        def fake_camc(*, prompt, workspace, name, tag, output_file,
                      timeout_s, write_id_to=None):  # noqa: ARG001
            envelope = {"status": "success", "data": {"value": 99},
                        "error": None, "feedback": None,
                        "request_human": False}
            (Path(workspace) / output_file).write_text(json.dumps(envelope))
            return ("aid", envelope)

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_camc)

        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("tiny@0.1.0", run_dir, run_proj)
        assert rc == 0

        events = [json.loads(line) for line in
                  (run_dir / "trace.jsonl").read_text().splitlines() if line]
        first = events[0]
        ws = first.get("workflow_source")
        assert ws is not None
        assert ws["type"] == "package"
        assert ws["planner_invoked"] is False
        assert ws["package"] == "tiny@0.1.0"
        assert ws["content_digest"].startswith("sha256:")
        # And the legacy fields are still present for back-compat.
        assert first["package"]["name"] == "tiny"
        assert first["planner_invoked"] is False

        pkg.uninstall_package("tiny", "0.1.0")

    def test_status_surfaces_workflow_source(self, tmp_path):
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "workflow.yaml").write_text(yaml.safe_dump({
            "workflow": "x", "version": "1.1",
            "nodes": [{"id": "n", "goal": "g", "steps": ["s"],
                        "run": {"skill": "analyzer"}}],
        }, sort_keys=False))
        # Synthesize a workflow_started event with workflow_source.
        events = [{"step": 1, "ts": "t", "event": "workflow_started",
                   "workflow_source": {"type": "package",
                                        "planner_invoked": False,
                                        "package": "tiny@0.1.0",
                                        "content_digest": "sha256:cafe"}}]
        (rd / "trace.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n")
        s = _summarize_status(rd)
        assert s["workflow_source"]["type"] == "package"
        text = _render_status_human(s)
        assert "source: package" in text
        assert "planner_invoked=false" in text


class TestPackageRunMaterialization:
    """RFC §11 step 6/7 — package run materializes package-lock.json,
    preflight.json, workflow.yaml, and package skills into .camflow/run/."""

    def _stage(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        skills_dir = proj / "skills" / "analyzer"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Skill: analyzer\n")
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
             "reason": "initial_plan"}))
        return rd

    def test_package_run_writes_lock_and_preflight(self, tmp_path,
                                                     monkeypatch):
        from runner import runtime as rt
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls:
                                          Path(os.environ["HOME"])))

        rd_src = self._stage(tmp_path)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd_src, name="tiny", version="0.1.0",
                            out=archive)
        pkg.install_package(archive)

        def fake_camc(*, prompt, workspace, name, tag, output_file,
                      timeout_s, write_id_to=None):  # noqa: ARG001
            envelope = {"status": "success", "data": {"value": 1},
                        "error": None, "feedback": None,
                        "request_human": False}
            (Path(workspace) / output_file).write_text(json.dumps(envelope))
            return ("aid", envelope)

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_camc)

        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("tiny@0.1.0", run_dir, run_proj)
        assert rc == 0

        # Lock copied to run dir for replay.
        lock = json.loads((run_dir / "package-lock.json").read_text())
        assert lock["name"] == "tiny"
        assert lock["content_digest"].startswith("sha256:")

        # Preflight result written.
        pre = json.loads((run_dir / "preflight.json").read_text())
        assert pre["status"] == "ok"
        assert isinstance(pre["checks"], list)

        pkg.uninstall_package("tiny", "0.1.0")

    def test_package_run_fails_missing_required_command(self, tmp_path,
                                                        monkeypatch):
        from runner import runtime as rt
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setattr(Path, "home",
                             classmethod(lambda cls:
                                          Path(os.environ["HOME"])))

        rd_src = self._stage(tmp_path)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd_src, name="tiny", version="0.1.0",
                            out=archive)

        files = pkg._read_package_files(archive)
        manifest = yaml.safe_load(files["manifest.yaml"].decode("utf-8"))
        manifest["environment"] = {
            "required_commands": ["__camflow_missing_command__"],
        }
        files["manifest.yaml"] = yaml.safe_dump(
            manifest, sort_keys=False).encode("utf-8")
        digest_map = {p: pkg._file_sha256(b)
                      for p, b in files.items() if p != "lock.json"}
        lock = json.loads(files["lock.json"].decode("utf-8"))
        lock["files"] = digest_map
        lock["content_digest"] = pkg.canonical_tree_digest(digest_map)
        files["lock.json"] = (json.dumps(lock, indent=2, sort_keys=True)
                              + "\n").encode("utf-8")
        broken = tmp_path / "needs-cmd.camflowpkg"
        pkg._write_archive(broken, files)
        pkg.install_package(broken)

        run_proj = tmp_path / "live"
        run_dir = run_proj / ".camflow" / "run"
        run_dir.mkdir(parents=True)
        rc = rt._run_packaged("tiny@0.1.0", run_dir, run_proj)

        assert rc == 1
        pre = json.loads((run_dir / "preflight.json").read_text())
        assert pre["status"] == "fail"
        assert pre["checks"][0]["kind"] == "required_command"
        assert pre["checks"][0]["name"] == "__camflow_missing_command__"
        assert not (run_dir / "trace.jsonl").exists()

        pkg.uninstall_package("tiny", "0.1.0")


class TestProjectInstallBookkeeping:
    """RFC §9 — project-local installs maintain
    `<project>/.camflow/package-lock.json` + append-only
    `<project>/.camflow/install.log`. State stays under .camflow/."""

    def test_project_install_writes_lock_and_log(self, tmp_path):
        rd = _stage_successful_run(tmp_path)
        archive = tmp_path / "tiny.camflowpkg"
        pkg.create_package(run_dir=rd, name="tiny", version="0.1.0",
                            out=archive)
        proj = tmp_path / "myproj"
        proj.mkdir()
        pkg.install_package(archive, project_local=True,
                             project_root=proj)

        # Project-wide package-lock.json appears.
        proj_lock_path = proj / ".camflow" / "package-lock.json"
        assert proj_lock_path.is_file()
        proj_lock = json.loads(proj_lock_path.read_text())
        assert proj_lock["package_lock_schema"] == "1"
        assert any(p["name"] == "tiny" and p["version"] == "0.1.0"
                   for p in proj_lock["packages"])

        # install.log gained an "install" record.
        log_path = proj / ".camflow" / "install.log"
        assert log_path.is_file()
        records = [json.loads(line)
                    for line in log_path.read_text().splitlines() if line]
        assert any(r["action"] == "install" and r["name"] == "tiny"
                   and r["version"] == "0.1.0" for r in records)

        # Uninstall updates lock + appends to log.
        assert pkg.uninstall_package("tiny", "0.1.0", project_local=True,
                                      project_root=proj) is True
        proj_lock = json.loads(proj_lock_path.read_text())
        assert not any(p["name"] == "tiny" and p["version"] == "0.1.0"
                       for p in proj_lock["packages"])
        records = [json.loads(line)
                    for line in log_path.read_text().splitlines() if line]
        assert any(r["action"] == "uninstall" and r["name"] == "tiny"
                   for r in records)


class TestReplanPackagePolicy:
    """RFC §12 tightening — package-aware replan must fail before node
    execution if the new user_spec references undeclared skills/tools."""

    def _stub_planner_returning(self, monkeypatch, new_yaml: str):
        from runner import runtime as rt
        orig_init = rt.Workflow.__init__

        class _StubWorkflow:
            def __init__(self, spec, run_dir, *, project_root=None,
                         resume=False, replan=False):
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
                         replan=False, project_root=None):
            rd = Path(run_dir).resolve()
            if "planner-rev" in rd.name:
                stub = _StubWorkflow(spec, rd, project_root=project_root,
                                     resume=resume, replan=replan)
                self.__class__ = _StubWorkflow
                self.__dict__.update(stub.__dict__)
                return
            orig_init(self, spec, rd, resume=resume, replan=replan,
                      project_root=project_root)

        monkeypatch.setattr(rt.Workflow, "__init__", patched_init)

    def test_replan_introducing_undeclared_skill_halts(self, tmp_path,
                                                         monkeypatch):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
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
                "run": {"skill": "analyzer"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "false"},
                "retry": 1,
            }],
        }
        # Replan pulls in the camflow repo's `reviewer` skill (which
        # exists on disk so v1.1 schema validation passes), but it
        # is NOT declared in the package's manifest below — so the
        # package-policy gate is what must catch it.
        replan_yaml = yaml.safe_dump({
            "workflow": "rev2", "version": "1.1",
            "goal": "g.",
            "nodes": [{
                "id": "y", "goal": "g", "steps": ["s"],
                "run": {"skill": "reviewer"},
                "output_schema": {"approved": "boolean"},
                "verify": {"command": "true"},
                "retry": 1,
            }],
        }, sort_keys=False)
        self._stub_planner_returning(monkeypatch, replan_yaml)

        pkg_meta = {"name": "tiny", "version": "0.1.0",
                     "content_digest": "sha256:dead"}
        pkg_manifest = {
            "package_schema": "1", "name": "tiny", "version": "0.1.0",
            # Only "analyzer" declared — "reviewer" is undeclared.
            "skills": {"analyzer": {"path": "skills/analyzer/SKILL.md"}},
        }
        result = _execute_with_optional_auto_replan(
            initial, rd, package_meta=pkg_meta,
            package_manifest=pkg_manifest)
        assert result == "halted"
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["envelope"]["error"]["code"] == "PACKAGE_POLICY"
        assert "reviewer" in halt["envelope"]["error"]["message"]

    def test_package_replan_validates_materialized_run_dir_skills(
            self, tmp_path, monkeypatch):
        from runner import runtime as rt

        proj = tmp_path / "proj"
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("p")
        skill_dir = rd / "skills" / "pkgonly"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# package-only skill marker\n")

        initial = {
            "workflow": "rev1", "version": "1.1",
            "goal": "g.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"skill": "pkgonly"},
                "output_schema": {"value": "integer"},
                "retry": 0,
            }],
        }
        replan_yaml = yaml.safe_dump({
            "workflow": "rev2", "version": "1.1",
            "goal": "g.",
            "nodes": [{
                "id": "y", "goal": "g", "steps": ["s"],
                "run": {"skill": "pkgonly"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
                "retry": 0,
            }],
        }, sort_keys=False)
        self._stub_planner_returning(monkeypatch, replan_yaml)

        calls: list[dict] = []

        def fake_camc(*, prompt, workspace, name, tag, output_file,
                      timeout_s, write_id_to=None):  # noqa: ARG001
            node_id = Path(workspace).parent.name
            calls.append({"node": node_id, "prompt": prompt})
            if node_id == "x":
                envelope = {
                    "status": "fail", "data": {},
                    "error": {"code": "NEEDS_REPLAN",
                              "message": "needs replan"},
                    "feedback": "needs replan",
                    "request_human": True,
                }
            else:
                envelope = {
                    "status": "success", "data": {"value": 2},
                    "error": None, "feedback": None,
                    "request_human": False,
                }
            (Path(workspace) / output_file).write_text(json.dumps(envelope))
            return ("aid", envelope)

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_camc)

        pkg_meta = {"name": "tiny", "version": "0.1.0",
                     "content_digest": "sha256:dead"}
        pkg_manifest = {
            "package_schema": "1", "name": "tiny", "version": "0.1.0",
            "skills": {"pkgonly": {"path": "skills/pkgonly/SKILL.md"}},
        }
        result = _execute_with_optional_auto_replan(
            initial, rd, project_root=rd, package_meta=pkg_meta,
            package_manifest=pkg_manifest)

        assert result == "done"
        assert [c["node"] for c in calls] == ["x", "y"]
        assert "package-only skill marker" in calls[1]["prompt"]

    def test_replan_introducing_tool_node_halts(self, tmp_path,
                                                monkeypatch):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        _make_executable(scripts / "bad.sh", _envelope_tool(value=0))
        _make_executable(scripts / "ok.sh", _envelope_tool(value=1))
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("p")
        initial = {
            "workflow": "rev1", "version": "1.1",
            "goal": "g.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"skill": "analyzer"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "false"},
                "retry": 1,
            }],
        }
        # Replanned spec uses a tool node. Active workflows reject this
        # before package execution.
        replan_yaml = yaml.safe_dump({
            "workflow": "rev2", "version": "1.1",
            "goal": "g.",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/ok.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
                "retry": 1,
            }],
        }, sort_keys=False)
        self._stub_planner_returning(monkeypatch, replan_yaml)
        from runner import runtime as rt
        monkeypatch.setattr(
            rt, "exec_skill",
            lambda *a, **k: {
                "status": "success",
                "data": {"value": 1},
                "error": None,
                "feedback": None,
                "request_human": False,
            },
        )

        pkg_meta = {"name": "tiny", "version": "0.1.0",
                     "content_digest": "sha256:dead"}
        pkg_manifest = {
            "package_schema": "1", "name": "tiny", "version": "0.1.0",
            "skills": {},
        }
        # Tool node in replan → halt before node execution.
        result = _execute_with_optional_auto_replan(
            initial, rd, package_meta=pkg_meta,
            package_manifest=pkg_manifest)
        assert result == "halted"
        halt = json.loads((rd / "halt.json").read_text())
        assert "run.tool" in halt["envelope"]["error"]["message"]
