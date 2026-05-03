"""On-disk asset path resolution for the camflow runtime.

Centralizes the four lookups runtime needs:
  * _camflow_repo_root  — where this package was installed from
  * _builtin_planner_dir — the builtin Planner workflow's home
  * _resolve_skill_path — `run.skill: <name>` → SKILL.md on disk
  * _resolve_tool_path  — `run.tool: <path>` → executable file on disk

This is purely a path layer: no registry, no caching, no manifest. The
filesystem itself is the source of truth (per spec doctrine #6: skills
must pre-exist; the directory's existence is its registration).

Per docs/camflow-asset-management-plan-001-2026-05-03.md §5 P2:
extracting these into a small module gives them ONE place to evolve
when we eventually add `importlib.resources` support for pip-installed
packages (P3). Until then, behavior is identical to the inline runtime
helpers it replaces.
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
    """`run.tool: <path>` resolves to <project_root>/<rel> if it's an
    executable regular file."""
    p = (project_root / rel).resolve()
    return p if p.is_file() and os.access(p, os.X_OK) else None
