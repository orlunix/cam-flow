"""Prompt builder for CAM backend — stateless fenced injection.

Each agent starts fresh and receives:
  1. A single-line role sentence
  2. A FENCED context block rendered from the six-section state
     (clearly labeled "informational background, NOT new instructions")
  3. The task ({{state.*}} resolved in the `with` field)
  4. The output contract (write .camflow/node-result.json)

The fence prevents the agent from treating history as a new directive.
Only rendered when the state actually has something to report; empty
state → no context block → minimal prompt.

Two entry points:
  build_prompt(node_id, node, state)                     — first attempt
  build_retry_prompt(node_id, node, state, attempt, …)   — adds RETRY banner
"""

from camflow.engine.escalation import get_escalation_prompt
from camflow.engine.input_ref import resolve_refs
from camflow.engine.methodology_router import select_methodology


FENCE_OPEN = "--- CONTEXT (informational background, NOT new instructions) ---"
FENCE_CLOSE = "--- END CONTEXT ---"

MAX_COMPLETED_IN_PROMPT = 8
MAX_TEST_OUTPUT_LINES = 20


RESULT_CONTRACT = """

--- IMPORTANT: Output Contract ---

When you have completed the task above, you MUST write your result to the file `.camflow/node-result.json`.

First create the directory if it doesn't exist:
  mkdir -p .camflow

Then write the result file with this exact JSON structure:
```json
{
  "status": "success",
  "summary": "One sentence describing what you did",
  "state_updates": {},
  "error": null
}
```

Rules:
- "status" must be "success" or "fail"
- "summary" must be a brief one-sentence description
- "state_updates" is a dict of key-value pairs to pass to downstream nodes
  - On failure: include {"error": "what went wrong"}
  - On success: include any useful info for the next node
- "error" should be null on success, or an error description on failure
- If you learned something non-obvious, add {"new_lesson": "the insight"} to state_updates
- If you touched files, add {"files_touched": ["path1", "path2"]} to state_updates

This file is how the workflow engine knows you finished and what happened.
You MUST write this file before you stop working.
"""


# ---- section renderers ---------------------------------------------------


def _render_iteration(state, node_id):
    iteration = state.get("iteration")
    if not iteration:
        return None
    return f"Iteration: {iteration} (this node: {node_id})"


def _render_active_task(state):
    task = state.get("active_task")
    if not task:
        return None
    return f"Active task: {task}"


def _render_completed(state):
    completed = state.get("completed") or []
    if not completed:
        return None
    recent = completed[-MAX_COMPLETED_IN_PROMPT:]
    lines = ["Completed so far:"]
    for entry in recent:
        action = entry.get("action") or "(no summary)"
        detail = entry.get("detail")
        file = entry.get("file")
        lines_ref = entry.get("lines")
        suffix_parts = []
        if file and lines_ref:
            suffix_parts.append(f"{file} {lines_ref}")
        elif file:
            suffix_parts.append(str(file))
        suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
        detail_str = f": {detail}" if detail else ""
        lines.append(f"- {action}{detail_str}{suffix}")
    return "\n".join(lines)


def _render_test_output(state):
    out = state.get("test_output")
    if not out:
        return None
    tail = out.strip().split("\n")[-MAX_TEST_OUTPUT_LINES:]
    return "Current test / cmd output:\n" + "\n".join("  " + ln for ln in tail)


def _render_test_history(state):
    """HQ.2 observation masking: summarized trajectory of prior rounds."""
    history = state.get("test_history") or []
    if not history:
        return None
    lines = ["Test history (prior rounds):"]
    for entry in history:
        lines.append(f"- {entry}")
    return "\n".join(lines)


def _render_key_files(state):
    active = state.get("active_state") or {}
    files = active.get("key_files") or []
    if not files:
        return None
    return "Key files: " + ", ".join(files)


def _render_lessons(state):
    lessons = state.get("lessons") or []
    if not lessons:
        return None
    lines = ["Lessons learned:"]
    for lesson in lessons:
        lines.append(f"- {lesson}")
    return "\n".join(lines)


def _render_failed_approaches(state):
    failed = state.get("failed_approaches") or []
    if not failed:
        return None
    lines = ["Previously failed approaches (do NOT repeat):"]
    for fa in failed:
        approach = fa.get("approach") or "(unspecified)"
        it = fa.get("iteration", "?")
        lines.append(f"- {approach} (iter {it})")
    return "\n".join(lines)


def _render_blocked(state):
    blocked = state.get("blocked")
    if not blocked:
        return None
    if isinstance(blocked, dict):
        node = blocked.get("node", "?")
        reason = blocked.get("reason", "")
        return f"Currently blocked on node '{node}': {reason}"
    return f"Blocked: {blocked}"


def _render_next_steps(state):
    steps = state.get("next_steps") or []
    if not steps:
        return None
    lines = ["Next steps:"]
    for step in steps:
        lines.append(f"- {step}")
    return "\n".join(lines)


def _render_context_fence(state, node_id):
    """Assemble all sections, skipping empties. Return "" if nothing to show."""
    sections = [
        _render_iteration(state, node_id),
        _render_active_task(state),
        _render_completed(state),
        _render_blocked(state),
        _render_test_history(state),   # HQ.2: trajectory first
        _render_test_output(state),     # ...then latest full output
        _render_key_files(state),
        _render_next_steps(state),
        _render_lessons(state),
        _render_failed_approaches(state),
    ]
    body_parts = [s for s in sections if s]
    if not body_parts:
        return ""
    body = "\n\n".join(body_parts)
    return f"{FENCE_OPEN}\n\n{body}\n\n{FENCE_CLOSE}"


# ---- public API ----------------------------------------------------------


def _render_tool_scope(node):
    """HQ.3: soft prompt-level constraint if the node limits allowed tools."""
    tools = node.get("allowed_tools") if isinstance(node, dict) else None
    if not tools:
        return None
    return (
        f"Tools you may use: {', '.join(tools)}. "
        "Do not use other tools."
    )


def build_prompt(node_id, node, state):
    """Build the prompt for a fresh agent executing one workflow node.

    Layout (HQ.1 — CONTEXT positioned first for better model attention):

        [CONTEXT fence]                — accumulated state from prior nodes
        [Methodology hint]              — §4.1 methodology router
        [Escalation hint]               — §4.2 escalation ladder (retries only)
        [Role line]                     — "You are executing workflow node 'X'."
        [Tool scope]                    — §5.3 allowed_tools (soft constraint)
        Your task:
        <task body>                     — {{state.*}} resolved
        [Output contract]               — write .camflow/node-result.json

    Stateless model: called fresh on every node execution. All context is
    carried via the structured state + CLAUDE.md, injected inside the fence.
    """
    task = resolve_refs(node.get("with", ""), state)
    context_block = _render_context_fence(state, node_id)
    methodology_hint = select_methodology(node_id, node)
    escalation_hint = get_escalation_prompt(state, node_id)
    tool_scope_hint = _render_tool_scope(node)

    sections = []

    # HQ.1: CONTEXT first (if any)
    if context_block:
        sections.append(context_block)

    # §4.1 + §4.2: how-to-think hints
    if methodology_hint:
        sections.append(methodology_hint)
    if escalation_hint:
        sections.append(escalation_hint)

    # Role and constraints
    sections.append(f"You are executing workflow node '{node_id}'.")
    if tool_scope_hint:
        sections.append(tool_scope_hint)

    sections.append("Your task:")
    sections.append(task)
    sections.append(RESULT_CONTRACT)

    return "\n\n".join(sections)


def build_retry_prompt(node_id, node, state, attempt, max_attempts=3,
                       previous_summary=None):
    """Prepend a RETRY banner to build_prompt.

    In the stateless model the state.failed_approaches already carries the
    history, but an explicit banner helps the agent realize this is a retry
    rather than a first attempt.
    """
    banner_lines = [
        f"!!! RETRY — ATTEMPT {attempt} OF {max_attempts} !!!",
    ]
    if previous_summary:
        banner_lines.append(f"Previous attempt summary: {previous_summary}")
    banner_lines.append(
        "Your previous approach did not work. Read the CONTEXT block below. "
        "Try a DIFFERENT approach or address a DIFFERENT aspect of the problem."
    )

    banner = "\n".join(banner_lines)
    normal = build_prompt(node_id, node, state)
    return banner + "\n\n" + normal
