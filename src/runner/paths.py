"""Run dir layout + run id.

A camflow project keeps exactly ONE current run on disk plus a rolling
archive of past runs:

  <project>/.camflow/run/                          ← current run
  <project>/.camflow/archives/<stamp>-<status>/    ← past runs

When a new run starts, the previous .camflow/run/ (if any) is moved to
archives/ before a fresh run/ is created. No timestamped run dir per
invocation; no nesting noise.

Public API:
- `default_run_dir(project_root)` — return .camflow/run/, auto-archiving
  any prior run.
- `gen_run_id()` — short timestamp+hex string used to tag camc agents
  with `camflow:<run_id>`.
- `utcnow_iso()` — short ISO timestamp for trace events.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path


_RUN_DIRNAME = "run"
_ARCHIVES_DIRNAME = "archives"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gen_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def project_camflow_dir(project_root: Path) -> Path:
    return project_root / ".camflow"


def default_run_dir(project_root: Path) -> Path:
    """Return <project>/.camflow/run/, archiving any prior run first."""
    cam = project_camflow_dir(project_root)
    run = cam / _RUN_DIRNAME
    if run.exists() and any(run.iterdir()):
        archive_run_dir(run, cam / _ARCHIVES_DIRNAME)
    run.mkdir(parents=True, exist_ok=True)
    return run


def archive_run_dir(run_dir: Path, archives_root: Path) -> Path | None:
    """Move run_dir to archives_root/<stamp>-<status>/. Best-effort.

    Status suffix comes from halt.json or trace tail so the dir name
    tells the outcome at a glance (success / failure / halted / unknown).
    """
    if not run_dir.exists():
        return None
    archives_root.mkdir(parents=True, exist_ok=True)
    status = _peek_run_status(run_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Optional CAMFLOW_ARCHIVE_SUFFIX lets a regression / batch run tag
    # all its archives with an identifier (e.g. "regression-20260501").
    extra = os.environ.get("CAMFLOW_ARCHIVE_SUFFIX", "").strip()
    suffix = f"-{extra}" if extra else ""
    target = archives_root / f"{stamp}-{status}{suffix}"
    n = 1
    while target.exists():
        target = archives_root / f"{stamp}-{status}{suffix}-{n}"
        n += 1
    run_dir.rename(target)
    return target


def _peek_run_status(run_dir: Path) -> str:
    """Best-effort: success / failure / halted / unknown."""
    if (run_dir / "halt.json").exists():
        return "halted"
    trace = run_dir / "trace.jsonl"
    if trace.exists():
        try:
            lines = trace.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1])
                if last.get("event") == "workflow_completed":
                    return last.get("status") or "unknown"
        except Exception:
            pass
    # plan-mode parent dir: aggregate over planner/ + main/ children.
    for sub in ("main", "planner"):
        if (run_dir / sub).is_dir():
            s = _peek_run_status(run_dir / sub)
            if s != "unknown":
                return s
    return "unknown"
