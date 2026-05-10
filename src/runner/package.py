"""CamFlow v1.2 packaged workflows — P0 implementation.

Build / validate / install / inspect / list / uninstall packaged
workflows. The .camflowpkg artifact is a deterministic gzipped POSIX
tar with a fixed `camflowpkg/` root containing the frozen
workflow.yaml, manifest.yaml, lock.json, and bundled skills.

P0 limitations (explicit):
  * run.tool nodes are NOT supported in package create — the RFC's
    rewrite-to-tools/ path is deferred. create_package raises
    PackageError when the source workflow contains a run.tool node.
  * skill_resolution.allow_host_skills (RFC §13) is not surfaced;
    package skills are mandatory; the runtime resolves skills strictly
    from the package's `skills/` dir at execution time.
  * No remote registry, no signing.

Stdlib-only by design. The runtime never executes anything from a
package's `docs/` / `examples/` / `evidence/` directories — those are
descriptive metadata.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Iterable, Optional

import yaml


# ─── Constants ────────────────────────────────────────────────────────

PACKAGE_SCHEMA = "1"
WORKFLOW_SPEC = "1.1"
DIGEST_ALGORITHM = "camflow-tree-sha256-v1"
ARCHIVE_ROOT = "camflowpkg"
LOCK_FILENAME = "lock.json"
MANIFEST_FILENAME = "manifest.yaml"
WORKFLOW_FILENAME = "workflow.yaml"
INSTALLED_METADATA = "installed.json"
PACKAGE_RUNTIME_FILE = "package.json"

# Bundled CamFlow runtime version that this code targets.
CAMFLOW_RUNTIME_VERSION = "1.2.0"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9_.-]+)?$")
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

# Allowed package-relative path families. Each entry is a regex that
# matches paths AFTER the camflowpkg/ root prefix has been stripped.
_PATH_PATTERNS = [
    re.compile(r"^manifest\.yaml$"),
    re.compile(r"^lock\.json$"),
    re.compile(r"^workflow\.yaml$"),
    re.compile(r"^skills/[a-z][a-z0-9_]{1,63}/SKILL\.md$"),
    re.compile(r"^tools/.+"),
    re.compile(r"^prompts/nodes/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/(run|verify)\.md$"),
    re.compile(r"^preflight/checks\.yaml$"),
    re.compile(r"^examples/.+"),
    re.compile(r"^evidence/.+"),
    re.compile(r"^docs/.+"),
]


class PackageError(Exception):
    """Raised on any package-related failure (create / validate /
    install). Errors are surfaced to the operator."""


# ─── Tree digest ──────────────────────────────────────────────────────

def _file_sha256(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return f"sha256:{h.hexdigest()}"


def canonical_tree_digest(files: dict[str, str]) -> str:
    """Compute the package content_digest from a path → sha256 map.

    Per RFC §7.1:
        content_digest = sha256(
          for each path in sorted(files.keys()):
            utf8(path) + "\\0" + utf8(files[path]) + "\\n"
        )
    """
    h = hashlib.sha256()
    for path in sorted(files.keys()):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(files[path].encode("utf-8"))
        h.update(b"\n")
    return f"sha256:{h.hexdigest()}"


# ─── Path / archive validation ────────────────────────────────────────

def _is_allowed_relpath(relpath: str) -> bool:
    """True iff relpath (camflowpkg/-stripped) matches an allowed family."""
    return any(p.match(relpath) for p in _PATH_PATTERNS)


def _check_archive_member(member: tarfile.TarInfo) -> Optional[str]:
    """Return an error string if the member is unsafe, else None."""
    name = member.name
    if not name:
        return "empty member name"
    if name.startswith("/"):
        return f"absolute path not allowed: {name!r}"
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return f"`..` not allowed in path: {name!r}"
    if member.issym() or member.islnk():
        return f"symlink/hardlink not allowed: {name!r}"
    if member.ischr() or member.isblk() or member.isfifo() or \
            member.isdev():
        return f"device/fifo not allowed: {name!r}"
    if not (member.isfile() or member.isdir()):
        return f"unsupported entry type for {name!r}"
    if not name.startswith(ARCHIVE_ROOT + "/") and name != ARCHIVE_ROOT:
        return (f"path not under {ARCHIVE_ROOT}/: {name!r}")
    return None


def _strip_root(path: str) -> Optional[str]:
    """Strip the leading camflowpkg/ from a tar entry name."""
    if path == ARCHIVE_ROOT:
        return ""
    prefix = ARCHIVE_ROOT + "/"
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


# ─── Manifest validation ──────────────────────────────────────────────

_REQUIRED_MANIFEST_KEYS = {
    "package_schema", "name", "version", "workflow_spec",
    "workflow_entry", "runtime", "skills", "provenance",
}
_KNOWN_MANIFEST_KEYS = _REQUIRED_MANIFEST_KEYS | {
    "description", "authors", "tags", "parameters",
    "environment", "tools", "prompt_snapshots",
}


def _validate_manifest(manifest: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest is not a dict"]
    for k in _REQUIRED_MANIFEST_KEYS:
        if k not in manifest:
            errors.append(f"manifest missing required key: {k!r}")
    for k in manifest:
        if k not in _KNOWN_MANIFEST_KEYS:
            errors.append(f"manifest has unknown top-level key: {k!r}")
    if errors:
        return errors

    if manifest.get("package_schema") != PACKAGE_SCHEMA:
        errors.append(
            f"package_schema must be {PACKAGE_SCHEMA!r}, got "
            f"{manifest.get('package_schema')!r}")
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(
            f"name must match {NAME_RE.pattern}, got {name!r}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        errors.append(
            f"version must be SemVer-like, got {version!r}")
    if manifest.get("workflow_spec") != WORKFLOW_SPEC:
        errors.append(
            f"workflow_spec must be {WORKFLOW_SPEC!r} for P0")
    if manifest.get("workflow_entry") != WORKFLOW_FILENAME:
        errors.append(
            f"workflow_entry must be {WORKFLOW_FILENAME!r} for P0")
    rt = manifest.get("runtime") or {}
    if not isinstance(rt, dict) or "min_camflow" not in rt:
        errors.append("runtime.min_camflow is required")
    if rt.get("planner_required_for_initial_run") is True:
        errors.append("runtime.planner_required_for_initial_run must "
                      "be false for direct-execution packages")
    if rt.get("package_local_skills") is False:
        errors.append("runtime.package_local_skills must not be false "
                      "in P0 (host fallback isn't supported)")
    skills = manifest.get("skills") or {}
    if not isinstance(skills, dict):
        errors.append("skills must be a mapping")
    else:
        for sk_name, decl in skills.items():
            if not SKILL_NAME_RE.match(sk_name):
                errors.append(
                    f"skill name {sk_name!r} doesn't match "
                    f"{SKILL_NAME_RE.pattern}")
            if not isinstance(decl, dict):
                errors.append(f"skills.{sk_name} must be a mapping")
                continue
            expected_path = f"skills/{sk_name}/SKILL.md"
            if decl.get("path") != expected_path:
                errors.append(
                    f"skills.{sk_name}.path must be {expected_path!r}")
    return errors


# ─── Lock validation ──────────────────────────────────────────────────

_REQUIRED_LOCK_KEYS = {
    "package_schema", "name", "version", "content_digest",
    "digest_algorithm", "files", "created",
}


def _validate_lock(lock: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(lock, dict):
        return ["lock is not a dict"]
    for k in _REQUIRED_LOCK_KEYS:
        if k not in lock:
            errors.append(f"lock missing required key: {k!r}")
    if errors:
        return errors
    if lock.get("package_schema") != PACKAGE_SCHEMA:
        errors.append(
            f"lock package_schema must be {PACKAGE_SCHEMA!r}")
    if lock.get("digest_algorithm") != DIGEST_ALGORITHM:
        errors.append(
            f"lock digest_algorithm must be {DIGEST_ALGORITHM!r}")
    files = lock.get("files")
    if not isinstance(files, dict):
        errors.append("lock files must be a mapping")
    else:
        for path, dig in files.items():
            if not isinstance(path, str) or not _is_allowed_relpath(path):
                errors.append(f"lock file path not allowed: {path!r}")
            if not isinstance(dig, str) or not dig.startswith("sha256:"):
                errors.append(f"lock file {path!r}: bad digest")
    cd = lock.get("content_digest")
    if not isinstance(cd, str) or not cd.startswith("sha256:"):
        errors.append("lock content_digest must be a sha256: string")
    return errors


def _validate_workflow_shape(wf: dict, *,
                             project_root: Optional[Path] = None) -> list[str]:
    """Run the runtime's workflow validator without making package.py a
    top-level dependency of runtime.py.

    With `project_root`, this also checks local skill/tool references and
    verify.command dependency availability, matching the normal compile gate.
    Archive validation uses schema-only mode because package files are still
    in memory and package-local skills/tools are cross-checked below.
    """
    from .runtime import validate_workflow, validate_verify_command_dependencies

    errors = validate_workflow(wf, project_root=project_root)
    if project_root is not None:
        errors.extend(validate_verify_command_dependencies(
            wf, project_root=project_root))
    return errors


# ─── Reading packages ─────────────────────────────────────────────────

def _read_archive_entries(
    pkg_archive: Path,
) -> tuple[dict[str, bytes], list[str]]:
    """Read a .camflowpkg archive into a {relpath: bytes} dict.

    Returns (files, errors). Errors prevent reading; files is empty
    when errors are present.
    """
    errors: list[str] = []
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(pkg_archive, "r:gz") as tar:
            for member in tar.getmembers():
                err = _check_archive_member(member)
                if err:
                    errors.append(err)
                    continue
                rel = _strip_root(member.name)
                if rel is None:
                    errors.append(f"member outside {ARCHIVE_ROOT}/: "
                                  f"{member.name!r}")
                    continue
                if member.isdir():
                    continue
                if rel in seen:
                    errors.append(f"duplicate path in archive: {rel!r}")
                    continue
                seen.add(rel)
                if not _is_allowed_relpath(rel):
                    errors.append(f"path not in allowed family: {rel!r}")
                    continue
                f = tar.extractfile(member)
                if f is None:
                    errors.append(f"could not read archive member: "
                                  f"{member.name!r}")
                    continue
                files[rel] = f.read()
    except tarfile.TarError as e:
        errors.append(f"tar read error: {e}")
    except OSError as e:
        errors.append(f"archive read error: {e}")
    return files, errors


def _read_dir_entries(pkg_dir: Path) -> dict[str, bytes]:
    """Read an installed camflowpkg/ directory into {relpath: bytes}."""
    out: dict[str, bytes] = {}
    for p in pkg_dir.rglob("*"):
        if p.is_symlink():
            raise PackageError(
                f"symlink not allowed in installed package: {p}")
        if not p.is_file():
            continue
        rel = str(p.relative_to(pkg_dir)).replace(os.sep, "/")
        out[rel] = p.read_bytes()
    return out


def _read_package_files(target: Path) -> dict[str, bytes]:
    """Read package files from either a .camflowpkg archive or an
    already-installed camflowpkg/ directory."""
    target = Path(target)
    if target.is_file():
        files, errors = _read_archive_entries(target)
        if errors:
            raise PackageError("; ".join(errors))
        return files
    if target.is_dir():
        # If user passed `<install>/<name>/<version>/` walk down to camflowpkg.
        if (target / ARCHIVE_ROOT).is_dir():
            target = target / ARCHIVE_ROOT
        return _read_dir_entries(target)
    raise PackageError(f"package not found: {target}")


# ─── Validate ─────────────────────────────────────────────────────────

def validate_package(target: Path) -> list[str]:
    """Validate a package archive or installed directory.

    Returns a list of error strings; empty == valid.
    """
    target = Path(target)
    try:
        files = _read_package_files(target)
    except PackageError as e:
        return [str(e)]

    errors: list[str] = []
    for required in (MANIFEST_FILENAME, LOCK_FILENAME, WORKFLOW_FILENAME):
        if required not in files:
            errors.append(f"missing required file: {required}")
    if errors:
        return errors

    try:
        manifest = yaml.safe_load(files[MANIFEST_FILENAME].decode("utf-8"))
    except yaml.YAMLError as e:
        return [f"manifest.yaml is not valid YAML: {e}"]
    try:
        lock = json.loads(files[LOCK_FILENAME].decode("utf-8"))
    except json.JSONDecodeError as e:
        return [f"lock.json is not valid JSON: {e}"]

    errors.extend(_validate_manifest(manifest))
    errors.extend(_validate_lock(lock))
    if errors:
        return errors

    # Cross-check manifest ↔ lock.
    if manifest["name"] != lock["name"]:
        errors.append(
            f"manifest name {manifest['name']!r} != lock name "
            f"{lock['name']!r}")
    if manifest["version"] != lock["version"]:
        errors.append(
            f"manifest version {manifest['version']!r} != lock version "
            f"{lock['version']!r}")

    # Verify file digests.
    declared_files = lock["files"]
    file_relpaths = {p for p in files if p != LOCK_FILENAME}
    for rel in sorted(file_relpaths):
        if rel not in declared_files:
            errors.append(f"file not in lock: {rel!r}")
            continue
        actual = _file_sha256(files[rel])
        if actual != declared_files[rel]:
            errors.append(
                f"digest mismatch for {rel!r}: lock={declared_files[rel]} "
                f"actual={actual}")
    for rel in declared_files:
        if rel not in files:
            errors.append(f"lock declares missing file: {rel!r}")

    if errors:
        return errors

    # Recompute content digest.
    digest_input = {p: declared_files[p] for p in declared_files}
    expected = canonical_tree_digest(digest_input)
    if expected != lock["content_digest"]:
        errors.append(
            f"content_digest mismatch: declared "
            f"{lock['content_digest']!r} expected {expected!r}")
        return errors

    # Cross-check workflow.yaml's run.skill nodes are bundled. Strict —
    # no host fallback for P0.
    try:
        wf = yaml.safe_load(files[WORKFLOW_FILENAME].decode("utf-8"))
    except yaml.YAMLError as e:
        return [f"workflow.yaml is not valid YAML: {e}"]
    if not isinstance(wf, dict):
        return ["workflow.yaml top-level is not a dict"]
    workflow_errors = _validate_workflow_shape(wf)
    if workflow_errors:
        errors.extend(
            f"workflow.yaml validation failed: {e}"
            for e in workflow_errors)
    nodes = wf.get("nodes") or []
    declared_skills = manifest["skills"]
    for n in nodes:
        if not isinstance(n, dict):
            continue
        run = n.get("run") or {}
        sk = run.get("skill")
        if sk:
            if sk not in declared_skills:
                errors.append(
                    f"workflow node {n.get('id')!r} references "
                    f"skill {sk!r} not declared in manifest.skills")
                continue
            expected_path = f"skills/{sk}/SKILL.md"
            if expected_path not in files:
                errors.append(
                    f"manifest declares skill {sk!r} but bundled file "
                    f"{expected_path!r} is missing")
        tl = run.get("tool")
        if tl:
            errors.append(
                f"workflow node {n.get('id')!r} uses run.tool — P0 "
                f"packages do not yet support tool nodes (RFC §15)")
    return errors


# ─── Inspect ──────────────────────────────────────────────────────────

def inspect_package(target: Path) -> dict:
    """Return a structured summary of a package."""
    target = Path(target)
    files = _read_package_files(target)
    manifest = yaml.safe_load(files[MANIFEST_FILENAME].decode("utf-8"))
    lock = json.loads(files[LOCK_FILENAME].decode("utf-8"))
    return {
        "target": str(target),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "package_schema": manifest.get("package_schema"),
        "workflow_spec": manifest.get("workflow_spec"),
        "content_digest": lock.get("content_digest"),
        "skills": sorted((manifest.get("skills") or {}).keys()),
        "tools": list(manifest.get("tools") or []),
        "file_count": len(files),
        "min_camflow": (manifest.get("runtime") or {}).get("min_camflow"),
        "description": manifest.get("description"),
    }


# ─── Create ───────────────────────────────────────────────────────────

def _check_run_workflow(run_dir: Path) -> dict:
    """Read run_dir/workflow.yaml (the final live workflow)."""
    wf_path = run_dir / "workflow.yaml"
    if not wf_path.exists():
        raise PackageError(f"run dir missing workflow.yaml: {wf_path}")
    return yaml.safe_load(wf_path.read_text())


def _resolve_run_skill(skill_name: str,
                       project_root: Path,
                       repo_root: Path) -> Path:
    """Same lookup as runner.assets._resolve_skill_path, but raises on
    miss so the create error is surfaced cleanly to the operator."""
    for root in (project_root, repo_root):
        p = root / "skills" / skill_name / "SKILL.md"
        if p.exists():
            return p
    raise PackageError(
        f"skill {skill_name!r} required by run workflow not found in "
        f"project or repo skills/")


def create_package(*, run_dir: Path, name: str, version: str,
                   out: Path, project_root: Optional[Path] = None,
                   repo_root: Optional[Path] = None,
                   description: Optional[str] = None,
                   allow_halted: bool = False) -> Path:
    """Build a .camflowpkg from a finished run dir.

    Returns the path to the written archive. Raises PackageError on any
    soft failure (workflow halted, run.tool present, missing skill).
    """
    run_dir = Path(run_dir).resolve()
    out = Path(out).resolve()

    if not NAME_RE.match(name):
        raise PackageError(
            f"name {name!r} doesn't match {NAME_RE.pattern}")
    if not VERSION_RE.match(version):
        raise PackageError(
            f"version {version!r} not SemVer-like")

    # Source run must be successful.
    halt_path = run_dir / "halt.json"
    trace_path = run_dir / "trace.jsonl"
    last_event: Optional[dict] = None
    if trace_path.exists():
        for line in trace_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                last_event = json.loads(line)
            except json.JSONDecodeError:
                pass
    if halt_path.exists() and not allow_halted:
        raise PackageError(
            f"run dir is halted (halt.json present at {halt_path}); "
            f"pass allow_halted=True to override")
    if not allow_halted and (
            last_event is None
            or last_event.get("event") != "workflow_completed"
            or last_event.get("status") not in ("success", "done")):
        raise PackageError(
            "run dir is not a successfully completed workflow; "
            "package create requires final workflow_completed status=success")

    # Resolve project/repo roots.
    if project_root is None:
        parts = run_dir.parts
        if ".camflow" in parts:
            idx = parts.index(".camflow")
            project_root = Path(*parts[:idx]) if idx > 0 else Path("/")
        else:
            project_root = run_dir.parent
    if repo_root is None:
        # Two parents up from this file: src/runner/package.py → src → repo.
        repo_root = Path(__file__).resolve().parents[2]

    wf_spec = _check_run_workflow(run_dir)
    if not isinstance(wf_spec, dict):
        raise PackageError("run workflow.yaml top-level is not a dict")

    # Forbid run.tool nodes (P0 limitation).
    nodes = wf_spec.get("nodes") or []
    for n in nodes:
        if not isinstance(n, dict):
            raise PackageError("workflow.yaml has non-dict node")
        run = n.get("run") or {}
        if isinstance(run, dict) and "tool" in run:
            raise PackageError(
                f"node {n.get('id')!r} uses run.tool — P0 package "
                f"create does not support tool nodes; rewrite or "
                f"remove the tool node before packaging")

    workflow_errors = _validate_workflow_shape(
        wf_spec, project_root=project_root)
    if workflow_errors:
        raise PackageError(
            "run workflow.yaml is invalid: " + "; ".join(workflow_errors))

    skills_needed: dict[str, Path] = {}
    for n in nodes:
        run = n.get("run") or {}
        sk = run.get("skill")
        if sk and sk not in skills_needed:
            skills_needed[sk] = _resolve_run_skill(
                sk, project_root, repo_root)

    # Build files map.
    files: dict[str, bytes] = {}
    files[WORKFLOW_FILENAME] = yaml.safe_dump(
        wf_spec, sort_keys=False).encode("utf-8")
    for sk_name, sk_path in skills_needed.items():
        rel = f"skills/{sk_name}/SKILL.md"
        files[rel] = sk_path.read_bytes()

    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    manifest: dict = {
        "package_schema": PACKAGE_SCHEMA,
        "name": name,
        "version": version,
        "workflow_spec": WORKFLOW_SPEC,
        "workflow_entry": WORKFLOW_FILENAME,
        "runtime": {
            "min_camflow": CAMFLOW_RUNTIME_VERSION,
            "planner_required_for_initial_run": False,
            "package_local_skills": True,
        },
        "skills": {
            sk: {"path": f"skills/{sk}/SKILL.md",
                 "digest": _file_sha256(files[f"skills/{sk}/SKILL.md"])}
            for sk in sorted(skills_needed.keys())
        },
        "tools": [],
        "provenance": {
            "source_run_dir": str(run_dir),
            "created_at": created_at,
            "created_by": "camflow package create",
        },
    }
    if description:
        manifest["description"] = description
    files[MANIFEST_FILENAME] = yaml.safe_dump(
        manifest, sort_keys=False).encode("utf-8")

    # Build lock.json — files map + content_digest.
    digest_map = {p: _file_sha256(b) for p, b in files.items()}
    content_digest = canonical_tree_digest(digest_map)
    lock: dict = {
        "package_schema": PACKAGE_SCHEMA,
        "name": name,
        "version": version,
        "content_digest": content_digest,
        "digest_algorithm": DIGEST_ALGORITHM,
        "files": digest_map,
        "created": {
            "camflow_version": CAMFLOW_RUNTIME_VERSION,
            "python": sys.version.split()[0],
        },
    }
    files[LOCK_FILENAME] = (
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    # Write deterministic archive.
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_archive(out, files)
    return out


def _write_archive(out: Path, files: dict[str, bytes]) -> None:
    """Write the gzipped tar archive deterministically."""
    # First produce the tar bytes, then wrap in gzip with mtime=0.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w",
                      format=tarfile.USTAR_FORMAT) as tar:
        # Sort paths so the archive is deterministic.
        # Emit explicit directory entries for the root + subdirs so
        # extraction is well-defined.
        emitted_dirs: set[str] = set()

        def _add_dir(dirpath: str) -> None:
            if dirpath in emitted_dirs:
                return
            emitted_dirs.add(dirpath)
            ti = tarfile.TarInfo(name=dirpath)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti)

        _add_dir(ARCHIVE_ROOT)
        for relpath in sorted(files.keys()):
            # Add intermediate dirs.
            parts = relpath.split("/")
            for i in range(1, len(parts)):
                _add_dir(f"{ARCHIVE_ROOT}/" + "/".join(parts[:i]))
            content = files[relpath]
            ti = tarfile.TarInfo(name=f"{ARCHIVE_ROOT}/{relpath}")
            ti.type = tarfile.REGTYPE
            ti.mode = 0o644
            ti.size = len(content)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti, io.BytesIO(content))
    tar_bytes = tar_buf.getvalue()

    # Gzip with mtime=0 for determinism.
    with open(out, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(tar_bytes)


# ─── Install / list / uninstall ──────────────────────────────────────

def _install_root(*, project_local: bool,
                  project_root: Optional[Path] = None) -> Path:
    if project_local:
        if project_root is None:
            project_root = Path.cwd()
        return Path(project_root) / ".camflow" / "packages"
    return Path.home() / ".camflow" / "packages"


def install_package(archive: Path, *,
                    project_local: bool = False,
                    project_root: Optional[Path] = None,
                    replace: bool = False) -> Path:
    """Install a .camflowpkg archive. Returns the install path."""
    archive = Path(archive).resolve()
    errors = validate_package(archive)
    if errors:
        raise PackageError("; ".join(errors))

    files = _read_package_files(archive)
    manifest = yaml.safe_load(files[MANIFEST_FILENAME].decode("utf-8"))
    lock = json.loads(files[LOCK_FILENAME].decode("utf-8"))
    name = manifest["name"]
    version = manifest["version"]
    digest = lock["content_digest"]

    root = _install_root(project_local=project_local,
                         project_root=project_root)
    target_dir = root / name / version
    pkg_dir = target_dir / ARCHIVE_ROOT

    if target_dir.exists():
        # Reject digest-mismatched re-install of same name@version.
        existing = target_dir / INSTALLED_METADATA
        if existing.exists():
            try:
                meta = json.loads(existing.read_text())
            except json.JSONDecodeError:
                meta = {}
            if meta.get("content_digest") and meta["content_digest"] != digest:
                if not replace:
                    raise PackageError(
                        f"{name}@{version} already installed at "
                        f"{target_dir} with a DIFFERENT content_digest "
                        f"({meta['content_digest']!r}); refusing to "
                        f"silently replace. Pass replace=True to override.")
        if replace:
            shutil.rmtree(target_dir)

    pkg_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        outp = pkg_dir / rel
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_bytes(content)

    archive_digest = _file_sha256(archive.read_bytes())
    installed_at = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    meta = {
        "name": name,
        "version": version,
        "content_digest": digest,
        "archive_digest": archive_digest,
        "installed_at": installed_at,
        "installed_by_camflow": CAMFLOW_RUNTIME_VERSION,
        "source": str(archive),
    }
    (target_dir / INSTALLED_METADATA).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return target_dir


def list_installed(*, project_local: bool = False,
                   project_root: Optional[Path] = None) -> list[dict]:
    """List packages under the user or project install root."""
    root = _install_root(project_local=project_local,
                         project_root=project_root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for name_dir in sorted(root.iterdir()):
        if not name_dir.is_dir():
            continue
        for ver_dir in sorted(name_dir.iterdir()):
            if not ver_dir.is_dir():
                continue
            meta_path = ver_dir / INSTALLED_METADATA
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                continue
            meta["install_dir"] = str(ver_dir)
            out.append(meta)
    return out


def uninstall_package(name: str, version: str, *,
                      project_local: bool = False,
                      project_root: Optional[Path] = None) -> bool:
    """Remove an installed package. Returns True if something was
    removed, False if nothing matched."""
    root = _install_root(project_local=project_local,
                         project_root=project_root)
    target = root / name / version
    if not target.exists():
        return False
    shutil.rmtree(target)
    # Clean up empty parent.
    name_dir = root / name
    try:
        if name_dir.is_dir() and not any(name_dir.iterdir()):
            name_dir.rmdir()
    except OSError:
        pass
    return True


# ─── Resolver ─────────────────────────────────────────────────────────

def parse_package_id(pid: str) -> tuple[str, str]:
    """Parse `name@version` into (name, version). Raises on malformed."""
    if "@" not in pid:
        raise PackageError(
            f"package id must be NAME@VERSION (got {pid!r})")
    name, _, version = pid.partition("@")
    if not name or not version:
        raise PackageError(
            f"package id must be NAME@VERSION (got {pid!r})")
    if not NAME_RE.match(name):
        raise PackageError(f"invalid package name: {name!r}")
    if not VERSION_RE.match(version):
        raise PackageError(f"invalid package version: {version!r}")
    return name, version


def resolve_installed(name: str, version: str, *,
                      project_root: Optional[Path] = None) -> Path:
    """Resolve to the camflowpkg/ dir of an installed package.

    Order: project-local first, then user install. Raises PackageError
    if not found.
    """
    candidates: list[Path] = []
    if project_root is not None:
        proj = (Path(project_root) / ".camflow" / "packages"
                / name / version / ARCHIVE_ROOT)
        if proj.is_dir():
            candidates.append(proj)
    user = (Path.home() / ".camflow" / "packages"
            / name / version / ARCHIVE_ROOT)
    if user.is_dir():
        candidates.append(user)
    if not candidates:
        raise PackageError(
            f"package {name}@{version} not installed (looked under "
            f"./.camflow/packages and ~/.camflow/packages)")
    return candidates[0]


def read_install_metadata(install_dir: Path) -> dict:
    """Read installed.json from an install dir (parent of camflowpkg/)."""
    install_dir = Path(install_dir)
    meta_path = install_dir / INSTALLED_METADATA
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    # Fall back: try one dir up if caller passed camflowpkg/.
    if install_dir.name == ARCHIVE_ROOT:
        alt = install_dir.parent / INSTALLED_METADATA
        if alt.exists():
            return json.loads(alt.read_text())
    return {}
