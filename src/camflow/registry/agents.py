"""Agent registry — `.camflow/agents.json`.

Engine is the SOLE writer. Steward and CLI commands read but do not
write. Atomic write via temp + rename, mirroring ``state.json``.

Schema (see ``docs/design-next-phase.md`` §12.1):

    {
      "version": 1,
      "project_dir": "/abs/path",
      "current_steward_id": "steward-7c2a" | null,
      "agents": [
        {"id": "...", "role": "...", "status": "...", ...},
        ...
      ]
    }
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from camflow.backend.persistence import load_state, save_state_atomic

REGISTRY_FILE = "agents.json"
REGISTRY_VERSION = 1

ROLES = frozenset({"steward", "planner", "worker"})
STATUSES = frozenset({
    "alive",              # currently running
    "completed",          # finished cleanly (workers / planners)
    "failed",             # finished with an error result
    "killed",             # explicitly terminated (by steward, watchdog, or user)
    "handoff_archived",   # superseded by a fresh steward after compaction
})


def registry_path(project_dir: str | os.PathLike) -> str:
    """Return the absolute path of the registry file for a project."""
    return str(Path(project_dir) / ".camflow" / REGISTRY_FILE)


def _empty_registry(project_dir: str) -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "project_dir": str(Path(project_dir).resolve()),
        "current_steward_id": None,
        "agents": [],
    }


def load_registry(project_dir: str | os.PathLike) -> dict[str, Any]:
    """Load the registry, or return a fresh empty one if the file is missing.

    Never raises ``FileNotFoundError`` — callers can treat the returned
    dict as authoritative whether or not the file existed.
    """
    path = registry_path(project_dir)
    data = load_state(path, default=None)
    if data is None:
        return _empty_registry(str(project_dir))
    # Defensive: ensure every required key exists. A registry written
    # by an older version may be missing fields.
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("project_dir", str(Path(project_dir).resolve()))
    data.setdefault("current_steward_id", None)
    data.setdefault("agents", [])
    return data


def _save(project_dir: str | os.PathLike, registry: dict[str, Any]) -> None:
    save_state_atomic(registry_path(project_dir), registry)


def register_agent(project_dir: str | os.PathLike, agent: dict[str, Any]) -> None:
    """Append a new agent record. Raises if ``id`` already present.

    The caller supplies the agent dict; we validate the required fields
    (``id``, ``role``, ``status``) and write atomically.
    """
    if "id" not in agent:
        raise ValueError("agent dict requires 'id'")
    if agent.get("role") not in ROLES:
        raise ValueError(
            f"agent role must be one of {sorted(ROLES)}, got {agent.get('role')!r}"
        )
    if agent.get("status") not in STATUSES:
        raise ValueError(
            f"agent status must be one of {sorted(STATUSES)}, got {agent.get('status')!r}"
        )

    registry = load_registry(project_dir)
    if any(a.get("id") == agent["id"] for a in registry["agents"]):
        raise ValueError(f"agent id {agent['id']!r} already in registry")

    registry["agents"].append(dict(agent))
    _save(project_dir, registry)


def update_agent_status(
    project_dir: str | os.PathLike,
    agent_id: str,
    status: str,
    **extra_fields: Any,
) -> None:
    """Flip an agent's status and merge extra fields. Raises if not found.

    Typical use:
        update_agent_status(pdir, "camflow-build-a1b2c3", "completed",
                            completed_at="...", duration_ms=47000)
    """
    if status not in STATUSES:
        raise ValueError(
            f"status must be one of {sorted(STATUSES)}, got {status!r}"
        )

    registry = load_registry(project_dir)
    for agent in registry["agents"]:
        if agent.get("id") == agent_id:
            agent["status"] = status
            agent.update(extra_fields)
            _save(project_dir, registry)
            return

    raise KeyError(f"agent id {agent_id!r} not in registry")


def get_agent(
    project_dir: str | os.PathLike, agent_id: str
) -> dict[str, Any] | None:
    """Return the agent record, or ``None`` if not found."""
    for agent in load_registry(project_dir)["agents"]:
        if agent.get("id") == agent_id:
            return agent
    return None


def list_agents(
    project_dir: str | os.PathLike,
    role: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return agents filtered by role and/or status. Order = insertion order."""
    agents = load_registry(project_dir)["agents"]
    if role is not None:
        agents = [a for a in agents if a.get("role") == role]
    if status is not None:
        agents = [a for a in agents if a.get("status") == status]
    return list(agents)


def set_current_steward(
    project_dir: str | os.PathLike, agent_id: str | None
) -> None:
    """Set or clear ``current_steward_id``. Pass ``None`` to clear."""
    registry = load_registry(project_dir)
    if agent_id is not None and not any(
        a.get("id") == agent_id for a in registry["agents"]
    ):
        raise KeyError(
            f"steward id {agent_id!r} not in registry; "
            f"register the agent before setting current_steward_id"
        )
    registry["current_steward_id"] = agent_id
    _save(project_dir, registry)


def get_current_steward(
    project_dir: str | os.PathLike,
) -> dict[str, Any] | None:
    """Return the current Steward's agent record, or ``None``."""
    registry = load_registry(project_dir)
    sid = registry.get("current_steward_id")
    if sid is None:
        return None
    for agent in registry["agents"]:
        if agent.get("id") == sid:
            return agent
    return None


def append_flow_to_steward(
    project_dir: str | os.PathLike, flow_id: str,
) -> bool:
    """Idempotently append ``flow_id`` to the current Steward's
    ``flows_witnessed`` list. Returns True if the registry was
    written, False if there was no Steward, the flow was already
    listed, or anything else made the call a no-op.

    Project-scoped: a single Steward witnesses many flows over its
    lifetime. The list correlates the Steward back to the flows it
    saw — used by ``camflow steward status`` and Phase B's
    compaction-handoff boot pack.
    """
    if not flow_id:
        return False
    registry = load_registry(project_dir)
    sid = registry.get("current_steward_id")
    if not sid:
        return False
    for agent in registry["agents"]:
        if agent.get("id") != sid:
            continue
        flows = agent.setdefault("flows_witnessed", [])
        if flow_id in flows:
            return False
        flows.append(flow_id)
        _save(project_dir, registry)
        return True
    return False
