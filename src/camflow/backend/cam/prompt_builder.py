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

from camflow.engine.input_ref import resolve_refs


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
        _render_test_output(state),
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


def build_prompt(node_id, node, state):
    """Build the prompt for a fresh agent executing one workflow node.

    Note: this is called on every node execution — including retries — because
    the stateless model means each node gets a fresh agent. All context is
    carried via the structured state, injected inside the fence.
    """
    task = resolve_refs(node.get("with", ""), state)
    context_block = _render_context_fence(state, node_id)

    lines = [f"You are executing workflow node '{node_id}'."]
    if context_block:
        lines.append(context_block)
    lines.append("Your task:")
    lines.append(task)
    lines.append(RESULT_CONTRACT)

    return "\n\n".join(lines)


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
