"""Registry + trace hooks for agent lifecycle.

Single entry point that keeps the project-scoped registry
(``.camflow/agents.json``) consistent with the timeline trace
(``.camflow/trace.log``). Engine and orphan_handler call these
helpers; they should NOT touch the registry directly during the hot
path, so that any future invariant (e.g. "every status flip emits a
trace event") stays enforceable from one place.

Public API:
    on_agent_spawned(project_dir, role, agent_id, ...)   -> None
    on_agent_finalized(project_dir, agent_id, result, ...) -> None
    on_agent_killed(project_dir, agent_id, killed_by, reason, ...) -> None
    on_agent_handoff_archived(project_dir, agent_id, ...) -> None

Each one writes the registry first (atomic), then appends one trace
entry. If the registry write fails, no trace entry is appended — they
stay consistent or both fail.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from camflow.backend.cam.tracer import build_event_entry
from camflow.backend.persistence import append_trace_atomic
from camflow.registry.agents import (
    register_agent,
    update_agent_status,
)


def _trace_path(project_dir: str | os.PathLike) -> str:
    return str(Path(project_dir) / ".camflow" / "trace.log")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def _append_trace(project_dir: str | os.PathLike, entry: dict[str, Any]) -> None:
    append_trace_atomic(_trace_path(project_dir), entry)


# ---- spawn ---------------------------------------------------------------


def on_agent_spawned(
    project_dir: str | os.PathLike,
    *,
    role: str,
    agent_id: str,
    spawned_by: str,
    flow_id: str | None = None,
    node_id: str | None = None,
    tmux_session: str | None = None,
    prompt_file: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a new agent in the registry and emit ``agent_spawned`` trace.

    ``role`` is one of ``"steward"`` / ``"planner"`` / ``"worker"``.
    """
    spawned_at = _now_iso()
    record: dict[str, Any] = {
        "id": agent_id,
        "role": role,
        "status": "alive",
        "spawned_at": spawned_at,
        "spawned_by": spawned_by,
    }
    if flow_id is not None:
        record["flow_id"] = flow_id
    if node_id is not None:
        record["node_id"] = node_id
    if tmux_session is not None:
        record["tmux_session"] = tmux_session
    if prompt_file is not None:
        record["prompt_file"] = prompt_file
    if extra:
        record.update(extra)

    register_agent(project_dir, record)

    trace_fields: dict[str, Any] = {
        "agent_id": agent_id,
        "role": role,
        "spawned_by": spawned_by,
    }
    if node_id is not None:
        trace_fields["node_id"] = node_id
    if tmux_session is not None:
        trace_fields["tmux_session"] = tmux_session
    if prompt_file is not None:
        trace_fields["prompt_file"] = prompt_file

    _append_trace(
        project_dir,
        build_event_entry(
            "agent_spawned",
            actor="engine",
            flow_id=flow_id,
            ts=time.time(),
            **trace_fields,
        ),
    )


# ---- finalize (success / fail derived from result) ----------------------


def on_agent_finalized(
    project_dir: str | os.PathLike,
    *,
    agent_id: str,
    result: dict[str, Any],
    flow_id: str | None = None,
    duration_ms: int | None = None,
    completion_signal: str | None = None,
    result_file: str | None = None,
) -> None:
    """Update status based on ``result['status']`` and emit a trace event.

    Success → ``status="completed"``, ``kind="agent_completed"``.
    Failure → ``status="failed"``,    ``kind="agent_failed"``.
    """
    completed_at = _now_iso()
    is_success = (result or {}).get("status") == "success"
    new_status = "completed" if is_success else "failed"
    trace_kind = "agent_completed" if is_success else "agent_failed"

    extra_fields: dict[str, Any] = {"completed_at": completed_at}
    if duration_ms is not None:
        extra_fields["duration_ms"] = duration_ms
    if completion_signal is not None:
        extra_fields["completion_signal"] = completion_signal
    if result_file is not None:
        extra_fields["result_file"] = result_file
    if not is_success:
        err = (result or {}).get("error") or {}
        if err:
            extra_fields["error_code"] = err.get("code")

    update_agent_status(project_dir, agent_id, new_status, **extra_fields)

    trace_fields: dict[str, Any] = {"agent_id": agent_id}
    if duration_ms is not None:
        trace_fields["duration_ms"] = duration_ms
    if completion_signal is not None:
        trace_fields["completion_signal"] = completion_signal
    if result_file is not None:
        trace_fields["result_file"] = result_file
    if not is_success:
        err = (result or {}).get("error") or {}
        if err.get("code"):
            trace_fields["error_code"] = err["code"]

    _append_trace(
        project_dir,
        build_event_entry(
            trace_kind,
            actor="engine",
            flow_id=flow_id,
            ts=time.time(),
            **trace_fields,
        ),
    )


# ---- killed (explicit termination) --------------------------------------


def on_agent_killed(
    project_dir: str | os.PathLike,
    *,
    agent_id: str,
    killed_by: str,
    reason: str,
    flow_id: str | None = None,
    via: str | None = None,
) -> None:
    """Mark an agent killed and emit ``agent_killed`` trace.

    ``killed_by`` is the actor that initiated it (e.g.
    ``"steward-7c2a"``, ``"engine"``, ``"watchdog"``, ``"user"``).
    ``via`` describes the channel (e.g. ``"camflow ctl kill-worker"``,
    ``"orphan adoption"``).
    """
    killed_at = _now_iso()
    update_agent_status(
        project_dir,
        agent_id,
        "killed",
        killed_at=killed_at,
        killed_by=killed_by,
        killed_reason=reason,
    )

    trace_fields: dict[str, Any] = {
        "agent_id": agent_id,
        "killed_by": killed_by,
        "reason": reason,
    }
    if via is not None:
        trace_fields["via"] = via

    _append_trace(
        project_dir,
        build_event_entry(
            "agent_killed",
            actor=killed_by,
            flow_id=flow_id,
            ts=time.time(),
            **trace_fields,
        ),
    )


# ---- steward handoff ----------------------------------------------------


def on_agent_handoff_archived(
    project_dir: str | os.PathLike,
    *,
    agent_id: str,
    successor_id: str,
    memory_carried: list[str] | None = None,
) -> None:
    """Mark an old Steward archived after handoff and emit a trace event.

    Used during compaction handoff: the engine spawns a fresh Steward
    with the prior Steward's summary + archive as boot pack, then
    archives the old session.
    """
    archived_at = _now_iso()
    update_agent_status(
        project_dir,
        agent_id,
        "handoff_archived",
        archived_at=archived_at,
        successor_id=successor_id,
    )

    _append_trace(
        project_dir,
        build_event_entry(
            "handoff_completed",
            actor="engine",
            flow_id=None,  # project-level event
            ts=time.time(),
            from_agent=agent_id,
            to_agent=successor_id,
            memory_carried=list(memory_carried) if memory_carried else [],
        ),
    )
