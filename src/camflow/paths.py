"""Single source of truth for camflow filesystem layout.

Phase B introduced per-agent private directories so each agent (Steward,
Planner, Worker) has its own scratch / context / output namespace under
``.camflow/``. This module is the only place that knows the layout —
all callers use these helpers.

Layout::

    project_dir/.camflow/
    ├── state.json                 # canonical engine state (sole writer: engine)
    ├── agents.json                # registry            (sole writer: engine)
    ├── trace.log                  # audit timeline      (sole writer: engine)
    ├── workflow.yaml              # canonical workflow  (Planner produces; engine reads)
    ├── plan-rationale.md          # canonical rationale (Planner produces)
    ├── steward.json               # Steward pointer
    ├── node-result.json           # canonical "current node's result" (legacy contract;
    │                                Worker writes here, engine archives to per-attempt slot)
    ├── steward-events.jsonl       # mirror of every event the engine pushed to Steward
    ├── control.jsonl              # Phase B: drained verb queue
    ├── control-pending.jsonl      # Phase B: confirm-required verb queue
    ├── control-rejected.jsonl     # Phase B: rejected / timed-out
    ├── steward-config.yaml        # Phase B: autonomy config (user-owned)
    │
    ├── steward/                   # Steward private directory (one per project)
    │   ├── prompt.txt             # boot pack (regenerated only on handoff)
    │   ├── context.md             # IN — engine-written project briefing
    │   ├── summary.md             # OUT — Steward working memory
    │   ├── archive.md             # OUT — Steward condensed history
    │   ├── inbox.jsonl            # IN — events the engine pushed (mirror)
    │   ├── session.log            # camc capture mirror
    │   └── archive/               # Phase B compaction handoff: old Stewards
    │       └── <old-id>-<ts>/
    │           ├── prompt.txt
    │           ├── summary.md
    │           ├── archive.md
    │           └── session.log
    │
    └── flows/<flow_id>/           # one directory per engine flow
        ├── flow-summary.md        # engine-written, per-flow narrative
        ├── planner/               # Planner's private directory
        │   ├── prompt.txt
        │   ├── context.md         # IN
        │   ├── request.txt        # IN — user's NL request
        │   ├── workflow-draft.yaml
        │   ├── plan-rationale.md  # OUT — copied to canonical .camflow/plan-rationale.md
        │   ├── warnings.txt
        │   └── session.log
        ├── planner-replan-<n>/    # subsequent Planner spawns (replan)
        │   └── ... (same shape)
        └── nodes/<node_id>/
            ├── prompt.txt         # boot pack (per node — same across attempts)
            ├── context.md         # IN — regenerated each spawn
            ├── inputs/            # engine-written sibling outputs
            │   └── from-<prior-node>.json
            ├── result.json        # winning result (engine copies from latest attempt)
            └── attempts/<n>/
                ├── agent_id.txt   # whose attempt
                ├── progress.json  # OUT — worker's optional progress heartbeat
                ├── result.json    # OUT — worker's final result
                └── session.log

Design references:
  * docs/design-next-phase.md §12 — agent registry schema
  * docs/design-next-phase.md §13 — trace.log tagged-union
  * docs/strategy.md §9 — Steward agent
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


CAMFLOW_DIRNAME = ".camflow"


# ============================================================================
# Project root
# ============================================================================


def camflow_dir(project_dir: str | os.PathLike) -> Path:
    """``<project>/.camflow``. Created if missing."""
    p = Path(project_dir) / CAMFLOW_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(project_dir: str | os.PathLike) -> Path:
    return camflow_dir(project_dir) / "state.json"


def trace_path(project_dir: str | os.PathLike) -> Path:
    return camflow_dir(project_dir) / "trace.log"


def workflow_path(project_dir: str | os.PathLike) -> Path:
    """Canonical workflow.yaml — what the engine actually reads."""
    return camflow_dir(project_dir) / "workflow.yaml"


def plan_rationale_path(project_dir: str | os.PathLike) -> Path:
    return camflow_dir(project_dir) / "plan-rationale.md"


def node_result_path(project_dir: str | os.PathLike) -> Path:
    """Canonical "current node's result" — Worker writes here per the
    existing contract; engine archives the file into the per-attempt
    slot after reading it."""
    return camflow_dir(project_dir) / "node-result.json"


# ============================================================================
# Steward (one per project, persistent)
# ============================================================================


def steward_dir(project_dir: str | os.PathLike) -> Path:
    """``<project>/.camflow/steward/``. Created if missing."""
    p = camflow_dir(project_dir) / "steward"
    p.mkdir(parents=True, exist_ok=True)
    return p


def steward_prompt_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "prompt.txt"


def steward_context_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "context.md"


def steward_summary_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "summary.md"


def steward_archive_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "archive.md"


def steward_inbox_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "inbox.jsonl"


def steward_session_log_path(project_dir: str | os.PathLike) -> Path:
    return steward_dir(project_dir) / "session.log"


def steward_archive_subdir(
    project_dir: str | os.PathLike,
    old_agent_id: str,
    timestamp: str,
) -> Path:
    """Phase B compaction handoff: the old Steward's files folder.

    Named ``<old-id>-<UTC-ISO-without-ms-or-colons>/`` for sortability."""
    safe_ts = timestamp.replace(":", "").replace(".", "-")
    p = steward_dir(project_dir) / "archive" / f"{old_agent_id}-{safe_ts}"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ============================================================================
# Flow (one directory per engine flow)
# ============================================================================


def flow_dir(project_dir: str | os.PathLike, flow_id: str) -> Path:
    """``<project>/.camflow/flows/<flow_id>/``. Created if missing."""
    if not flow_id:
        raise ValueError("flow_id required for flow_dir")
    p = camflow_dir(project_dir) / "flows" / flow_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def flow_summary_path(project_dir: str | os.PathLike, flow_id: str) -> Path:
    return flow_dir(project_dir, flow_id) / "flow-summary.md"


# ============================================================================
# Planner (one per workflow generation; replans get -replan-N suffix)
# ============================================================================


def planner_dir(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    """``<flow>/planner/`` for the first Planner of this flow;
    ``<flow>/planner-replan-<n>/`` for subsequent replans."""
    base = flow_dir(project_dir, flow_id)
    name = "planner" if replan_n is None else f"planner-replan-{replan_n}"
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def planner_prompt_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return planner_dir(project_dir, flow_id, replan_n=replan_n) / "prompt.txt"


def planner_context_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return planner_dir(project_dir, flow_id, replan_n=replan_n) / "context.md"


def planner_request_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return planner_dir(project_dir, flow_id, replan_n=replan_n) / "request.txt"


def planner_draft_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return (
        planner_dir(project_dir, flow_id, replan_n=replan_n)
        / "workflow-draft.yaml"
    )


def planner_warnings_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return planner_dir(project_dir, flow_id, replan_n=replan_n) / "warnings.txt"


def planner_session_log_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    *,
    replan_n: int | None = None,
) -> Path:
    return planner_dir(project_dir, flow_id, replan_n=replan_n) / "session.log"


# ============================================================================
# Worker (per-node, per-attempt)
# ============================================================================


def node_dir(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> Path:
    if not node_id:
        raise ValueError("node_id required")
    p = flow_dir(project_dir, flow_id) / "nodes" / node_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def node_prompt_path(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> Path:
    """Boot pack for the node — same across attempts."""
    return node_dir(project_dir, flow_id, node_id) / "prompt.txt"


def node_context_path(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> Path:
    """``context.md`` regenerated per spawn (engine-written)."""
    return node_dir(project_dir, flow_id, node_id) / "context.md"


def node_inputs_dir(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> Path:
    p = node_dir(project_dir, flow_id, node_id) / "inputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def node_winning_result_path(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> Path:
    """The winning result for this node — engine copies from the
    latest successful attempt."""
    return node_dir(project_dir, flow_id, node_id) / "result.json"


def attempt_dir(
    project_dir: str | os.PathLike,
    flow_id: str,
    node_id: str,
    attempt_n: int,
) -> Path:
    """``<node>/attempts/<n>/``. ``attempt_n`` is 1-indexed."""
    if attempt_n < 1:
        raise ValueError(f"attempt_n must be >= 1, got {attempt_n}")
    p = (
        node_dir(project_dir, flow_id, node_id)
        / "attempts" / str(attempt_n)
    )
    p.mkdir(parents=True, exist_ok=True)
    return p


def attempt_agent_id_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    node_id: str,
    attempt_n: int,
) -> Path:
    return attempt_dir(project_dir, flow_id, node_id, attempt_n) / "agent_id.txt"


def attempt_progress_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    node_id: str,
    attempt_n: int,
) -> Path:
    return attempt_dir(project_dir, flow_id, node_id, attempt_n) / "progress.json"


def attempt_result_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    node_id: str,
    attempt_n: int,
) -> Path:
    return attempt_dir(project_dir, flow_id, node_id, attempt_n) / "result.json"


def attempt_session_log_path(
    project_dir: str | os.PathLike,
    flow_id: str,
    node_id: str,
    attempt_n: int,
) -> Path:
    return attempt_dir(project_dir, flow_id, node_id, attempt_n) / "session.log"


# ============================================================================
# Helpers for finding "the latest attempt"
# ============================================================================


def latest_attempt_n(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> int:
    """Highest existing attempt_n for the node, or 0 if no attempts yet."""
    attempts_root = node_dir(project_dir, flow_id, node_id) / "attempts"
    if not attempts_root.exists():
        return 0
    candidates: list[int] = []
    for child in attempts_root.iterdir():
        if not child.is_dir():
            continue
        try:
            candidates.append(int(child.name))
        except ValueError:
            continue
    return max(candidates) if candidates else 0


def next_attempt_n(
    project_dir: str | os.PathLike, flow_id: str, node_id: str,
) -> int:
    """The number to use for a fresh attempt directory (latest + 1)."""
    return latest_attempt_n(project_dir, flow_id, node_id) + 1
