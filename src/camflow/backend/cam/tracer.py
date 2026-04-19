"""Trace entry builder.

Produces a self-contained dict for one step of engine execution.
Written to trace.log as one JSONL line per step.

Entry schema (see docs/cam-phase-plan.md §7.1 + docs/evaluation.md §2):
  step, ts_start, ts_end, duration_ms, node_id, do, attempt, is_retry,
  retry_mode, input_state, node_result, output_state, transition,
  agent_id, exec_mode, completion_signal, lesson_added, event,
  # evaluation fields (see docs/evaluation.md):
  prompt_tokens, context_tokens, task_tokens,
  tools_available, tools_used, context_position,
  enricher_enabled, fenced, methodology, escalation_level
"""

import copy
from datetime import datetime, timezone


def _utc_iso(ts_float):
    """Convert a Unix timestamp (float) to ISO 8601 UTC with millisecond precision."""
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def approx_token_count(text):
    """Dependency-free token estimate: ~1 token per 4 characters.

    Deterministic and zero-dependency. Under-counts code by ~10% and
    over-counts prose by ~5% vs. a real tokenizer — consistent enough
    for trend measurement across runs. See docs/evaluation.md §2.1.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def build_trace_entry(
    step,
    node_id,
    node,
    input_state,
    node_result,
    output_state,
    transition,
    ts_start,
    ts_end,
    attempt=1,
    is_retry=False,
    retry_mode=None,
    agent_id=None,
    exec_mode="cmd",
    completion_signal=None,
    lesson_added=None,
    event=None,
    # ---- evaluation fields (all optional, defaults preserve behavior) ----
    prompt_tokens=None,
    context_tokens=None,
    task_tokens=None,
    tools_available=None,
    tools_used=None,
    context_position="middle",
    enricher_enabled=True,
    fenced=True,
    methodology="none",
    escalation_level=0,
):
    """Build a single trace entry.

    Deep-copies `input_state`, `output_state`, and `node_result` so later
    mutations don't corrupt the recorded snapshot.

    Evaluation fields default to values that describe current behavior
    (fenced=True, enricher_enabled=True, context_position="middle"). This
    keeps old callers working without code changes; new callers populate
    them for the evaluation framework.
    """
    return {
        "step": step,
        "ts_start": _utc_iso(ts_start),
        "ts_end": _utc_iso(ts_end),
        "duration_ms": int((ts_end - ts_start) * 1000),
        "node_id": node_id,
        "do": node.get("do", ""),
        "attempt": attempt,
        "is_retry": is_retry,
        "retry_mode": retry_mode,
        "input_state": copy.deepcopy(input_state),
        "node_result": copy.deepcopy(node_result) if node_result is not None else None,
        "output_state": copy.deepcopy(output_state),
        "transition": copy.deepcopy(transition) if transition is not None else None,
        "agent_id": agent_id,
        "exec_mode": exec_mode,
        "completion_signal": completion_signal,
        "lesson_added": lesson_added,
        "event": event,
        # Evaluation fields
        "prompt_tokens": prompt_tokens,
        "context_tokens": context_tokens,
        "task_tokens": task_tokens,
        "tools_available": tools_available,
        "tools_used": tools_used,
        "context_position": context_position,
        "enricher_enabled": enricher_enabled,
        "fenced": fenced,
        "methodology": methodology,
        "escalation_level": escalation_level,
    }
