"""On-disk asset path resolution for the camflow runtime.

Centralizes the runtime's on-disk lookups:
  * _camflow_repo_root   — where this package was installed from
  * _builtin_planner_dir — the builtin Planner workflow's home
  * _resolve_skill_path  — `run.skill: <name>` → SKILL.md on disk
  * _resolve_tool_path   — legacy compatibility helper for direct tests

v1.2 dropped `run.tool` as a workflow executor — workflows are
skill-only. Deterministic command paths live in `verify.command` and
are validated through `_resolve_command_path` inside runtime.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _camflow_repo_root() -> Path:
    """Where this runtime ships from — has builtin/ and skills/ as siblings.

    `Path(__file__)` is `<root>/src/runner/assets.py`, so two parents
    up is the repo root.
    """
    return Path(__file__).resolve().parents[2]


def _builtin_planner_dir() -> Path:
    """The builtin Planner workflow's directory."""
    return _camflow_repo_root() / "builtin" / "planner"


def _resolve_skill_path(name: str, project_root: Path) -> Optional[Path]:
    """Find <root>/skills/<name>/SKILL.md across two roots.

    Search order: the user's project root first, then the camflow repo
    root. The Planner workflow runs with project_root pointed at
    builtin/planner/ so its private skills (prompt_analyzer,
    workflow_designer, yaml_writer) win their lookup against the same
    name in shipped skills/ if any ever collided.
    """
    for root in (project_root, _camflow_repo_root()):
        p = root / "skills" / name / "SKILL.md"
        if p.exists():
            return p
    return None


def _resolve_tool_path(rel: str, project_root: Path) -> Optional[Path]:
    """Legacy tool-path resolver kept for internal compatibility tests.

    Active workflow YAML rejects `run.tool`; normal command execution
    belongs inside skills or `verify.command`. This helper still enforces
    containment for old direct runtime callers that bypass YAML validation.
    """
    if Path(rel).is_absolute():
        return None
    project_root_resolved = project_root.resolve()
    p = (project_root_resolved / rel).resolve()
    try:
        p.relative_to(project_root_resolved)
    except ValueError:
        return None
    return p if p.is_file() and os.access(p, os.X_OK) else None
