"""Event emitter — engine → Steward.

The engine pushes structured events to the Steward by calling
``camc send <steward-id> "[CAMFLOW EVENT] {json}"``. Events are also
mirrored to ``.camflow/steward-events.jsonl`` and written to
``trace.log`` as ``kind=event_emitted`` so we have full timeline
visibility.

The emitter is failure-tolerant: a dead Steward, a slow ``camc send``,
or a missing pointer file MUST NOT crash the engine main loop. We log
to engine.log on failure and continue.

Public API:
    emit(project_dir, event_type, **fields) -> bool
        True iff the event was sent to (a live) Steward.
        Always mirrors to disk regardless of send success.

Closed event-type set (matches docs/design-next-phase.md §7.3):
    flow_started, flow_terminal, flow_idle,
    node_started, node_done, node_failed,
    node_retry, escalation_level_change, verify_failed,
    heartbeat_stale_worker, replan_done, engine_resumed,
    checkpoint_now
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from camflow.backend.cam.tracer import build_event_entry
from camflow.backend.persistence import append_trace_atomic
from camflow.steward.spawn import is_steward_alive, load_steward_pointer


CAMC_BIN = shutil.which("camc") or "camc"

EVENTS_MIRROR_FILE = "steward-events.jsonl"
EVENT_PREFIX = "[CAMFLOW EVENT]"

EVENT_TYPES = frozenset({
    # flow lifecycle (Steward sees one engine instance per flow)
    "flow_started", "flow_terminal", "flow_idle",
    # node lifecycle
    "node_started", "node_done", "node_failed",
    "node_retry", "verify_failed",
    # engine state
    "escalation_level_change", "heartbeat_stale_worker",
    "replan_done", "engine_resumed", "checkpoint_now",
})


# ---- helpers ------------------------------------------------------------


def _utc_iso(ts_float: float) -> str:
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _camflow_dir(project_dir: str | os.PathLike) -> Path:
    p = Path(project_dir) / ".camflow"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _mirror_path(project_dir: str | os.PathLike) -> Path:
    return _camflow_dir(project_dir) / EVENTS_MIRROR_FILE


def _trace_path(project_dir: str | os.PathLike) -> Path:
    return _camflow_dir(project_dir) / "trace.log"


def _mirror_event(project_dir: str | os.PathLike, payload: dict[str, Any]) -> None:
    """Append the event to .camflow/steward-events.jsonl (durable)."""
    path = _mirror_path(project_dir)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _emit_trace(
    project_dir: str | os.PathLike,
    *,
    event_type: str,
    flow_id: str | None,
    payload_size: int,
    target: str | None,
    sent_ok: bool,
) -> None:
    append_trace_atomic(
        str(_trace_path(project_dir)),
        build_event_entry(
            "event_emitted",
            actor="engine",
            flow_id=flow_id,
            ts=time.time(),
            event_type=event_type,
            to=target,
            payload_size=payload_size,
            sent=sent_ok,
        ),
    )


# ---- camc send transport ------------------------------------------------


def _default_camc_send(agent_id: str, message: str) -> bool:
    """Default transport: ``camc send <agent_id> <message>``.

    Returns True on success, False on any error (we never raise — the
    engine main loop is upstream of every event emission).
    """
    try:
        proc = subprocess.run(
            [CAMC_BIN, "send", agent_id, message],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return proc.returncode == 0


# ---- emit ---------------------------------------------------------------


def emit(
    project_dir: str | os.PathLike,
    event_type: str,
    *,
    flow_id: str | None = None,
    camc_send: Callable[[str, str], bool] | None = None,
    log_failure: Callable[[str, Exception | None], None] | None = None,
    **fields: Any,
) -> bool:
    """Emit one event. Mirror to disk + write trace + try ``camc send``.

    Always:
      - validates ``event_type`` against ``EVENT_TYPES``;
      - mirrors the JSON to ``.camflow/steward-events.jsonl``;
      - emits a ``kind=event_emitted`` trace entry.

    Best-effort:
      - locates the current Steward and sends via ``camc send``;
      - if no Steward / send fails, returns False but does NOT raise.

    Returns True iff the message was successfully delivered to a live
    Steward. Mirroring + trace happen regardless.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"unknown event type {event_type!r}; "
            f"must be one of {sorted(EVENT_TYPES)}"
        )

    payload: dict[str, Any] = {
        "type": event_type,
        "flow_id": flow_id,
        "ts": _utc_iso(time.time()),
    }
    payload.update(fields)

    payload_json = json.dumps(payload, ensure_ascii=False)

    # 1. Mirror first (durable record even if send fails).
    try:
        _mirror_event(project_dir, payload)
    except Exception as exc:  # noqa: BLE001 — never block the engine
        if log_failure is not None:
            log_failure(f"event mirror failed for {event_type}", exc)

    # 2. Locate Steward and try to send.
    sent_ok = False
    target_id: str | None = None
    pointer = load_steward_pointer(project_dir)
    if pointer and pointer.get("agent_id"):
        target_id = pointer["agent_id"]
        send_fn = camc_send or _default_camc_send
        try:
            sent_ok = bool(
                send_fn(target_id, f"{EVENT_PREFIX} {payload_json}")
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            sent_ok = False
            if log_failure is not None:
                log_failure(
                    f"camc send failed for event {event_type} → {target_id}",
                    exc,
                )

    # 3. Trace the attempt regardless of success.
    try:
        _emit_trace(
            project_dir,
            event_type=event_type,
            flow_id=flow_id,
            payload_size=len(payload_json),
            target=target_id,
            sent_ok=sent_ok,
        )
    except Exception as exc:  # noqa: BLE001
        if log_failure is not None:
            log_failure(f"trace emit failed for {event_type}", exc)

    return sent_ok


# ---- convenience wrappers ---------------------------------------------


def emit_flow_started(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    workflow_path: str,
    steward_id: str | None = None,
    **kwargs: Any,
) -> bool:
    return emit(
        project_dir,
        "flow_started",
        flow_id=flow_id,
        workflow=workflow_path,
        steward=steward_id,
        **kwargs,
    )


def emit_flow_terminal(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    final: dict[str, Any],
    **kwargs: Any,
) -> bool:
    return emit(
        project_dir,
        "flow_terminal",
        flow_id=flow_id,
        final=final,
        **kwargs,
    )


def emit_node_started(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    step: int,
    node: str,
    agent_id: str | None = None,
    attempt: int = 1,
    **kwargs: Any,
) -> bool:
    return emit(
        project_dir,
        "node_started",
        flow_id=flow_id,
        step=step,
        node=node,
        agent_id=agent_id,
        attempt=attempt,
        **kwargs,
    )


def emit_node_done(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    step: int,
    node: str,
    summary: str,
    agent_id: str | None = None,
    **kwargs: Any,
) -> bool:
    return emit(
        project_dir,
        "node_done",
        flow_id=flow_id,
        step=step,
        node=node,
        status="success",
        summary=(summary or "")[:240],
        agent_id=agent_id,
        **kwargs,
    )


def emit_node_failed(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    step: int,
    node: str,
    summary: str,
    error: dict[str, Any] | None = None,
    agent_id: str | None = None,
    **kwargs: Any,
) -> bool:
    return emit(
        project_dir,
        "node_failed",
        flow_id=flow_id,
        step=step,
        node=node,
        status="fail",
        summary=(summary or "")[:240],
        error=error or {},
        agent_id=agent_id,
        **kwargs,
    )


def emit_engine_resumed(
    project_dir: str | os.PathLike,
    *,
    flow_id: str,
    pc: str | None = None,
    resumed_from: str | None = None,
    **kwargs: Any,
) -> bool:
    """Tell the Steward the engine just came back up — either via
    watchdog auto-restart or explicit ``camflow resume``. Steward
    uses this to re-orient: the previous "current node" may already
    be done (orphan adoption) or still in progress.
    """
    return emit(
        project_dir,
        "engine_resumed",
        flow_id=flow_id,
        pc=pc,
        resumed_from=resumed_from,
        **kwargs,
    )
