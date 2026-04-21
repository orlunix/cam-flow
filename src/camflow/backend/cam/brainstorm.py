"""Auto-brainstorm on repeated node failure.

When a node hits ``max_node_executions`` the engine would previously
give up with ``status=failed``. This module lets the engine take one
rescue attempt first: spawn a short brainstorm agent that looks at
the failure pattern, proposes a new strategy, and injects it back
into state so the next attempt of the same node sees the hint and
(hopefully) does something different.

This file owns the pure helpers (collect summaries, build the
prompt). The engine owns the I/O / agent spawn orchestration —
see ``Engine._trigger_brainstorm`` in ``engine.py``.
"""

from camflow.backend.persistence import load_trace


MAX_SUMMARIES = 10
TASK_PREVIEW = 600


def collect_failure_summaries(trace_path, node_id, limit=MAX_SUMMARIES):
    """Return the last ``limit`` failed trace entries for ``node_id``.

    Each element: ``{"step", "attempt", "summary", "error"}``. Reading
    from ``trace.log`` directly (rather than ``state.failed_approaches``)
    keeps this independent of state-shape churn — the trace is the
    append-only source of truth.
    """
    failures = []
    for entry in load_trace(trace_path):
        if entry.get("node_id") != node_id:
            continue
        nr = entry.get("node_result") or {}
        if nr.get("status") != "fail":
            continue
        err = nr.get("error")
        err_code = err.get("code") if isinstance(err, dict) else None
        failures.append({
            "step": entry.get("step"),
            "attempt": entry.get("attempt"),
            "summary": nr.get("summary") or "(no summary)",
            "error": err_code,
        })
    return failures[-limit:]


def build_brainstorm_prompt(node_id, node, failures, exec_count):
    """Compose the free-text prompt for the one-shot brainstorm agent.

    The brainstorm agent is NOT a normal workflow node — it runs
    outside the DSL transition graph, so it receives a hand-built
    prompt (no context fence, no methodology hint). The prompt is
    intentionally small: the failure summaries are the signal; the
    task here is pattern-recognition, not execution.
    """
    task_body = ""
    if isinstance(node, dict):
        task_body = (node.get("with") or node.get("do") or "").strip()
    task_preview = task_body[:TASK_PREVIEW]
    if len(task_body) > TASK_PREVIEW:
        task_preview += " …"

    lines = [
        f"Node '{node_id}' has failed {exec_count} times in a row and hit",
        "the workflow's max_node_executions limit.",
        "",
        "Failure summaries from trace.log (most recent last):",
    ]
    if not failures:
        lines.append("  (no failure summaries found in trace)")
    else:
        for f in failures:
            err_tag = f" [{f['error']}]" if f.get("error") else ""
            step = f.get("step", "?")
            attempt = f.get("attempt", "?")
            lines.append(
                f"  - step {step} attempt {attempt}: {f['summary']}{err_tag}"
            )

    lines.extend([
        "",
        "The node's task body is:",
        f"  {task_preview or '(empty)'}",
        "",
        "Your job: analyze the failure PATTERN (not individual failures),",
        "identify the ROOT CAUSE of repeated failure, and consider whether",
        "the current approach is fundamentally wrong. List 3 alternative",
        "approaches, then recommend ONE.",
        "",
        "Do NOT implement the fix. Do NOT edit any project files. Only",
        "diagnose + recommend.",
        "",
        "Output contract: write .camflow/node-result.json with",
        '  {"status": "success",',
        f'   "summary": "brainstorm for {node_id}: <one-line verdict>",',
        '   "state_updates": {"new_strategy": "<2-4 sentence instruction>"},',
        '   "error": null}',
        "",
        "The new_strategy string is prepended to the NEXT attempt of the",
        "failed node. Be concrete and actionable — e.g. name a specific",
        "file/function/flag to change, not just a direction.",
    ])
    return "\n".join(lines)
