"""State enrichment — merge node_result into the six-section schema.

Stateless execution model: each node runs in a fresh agent that sees
only CLAUDE.md + state.json. After every node executes, the engine
calls enrich_state() to merge the node_result into the structured
state so the NEXT node's prompt carries the accumulated context.

Six-section schema (rendered by prompt_builder in a fenced block):
  active_task       : what the workflow is trying to do right now
  completed         : history of successful actions (bounded list)
  active_state      : key_files / modified_files / cwd
  blocked           : current obstacle if any (null when no blockage)
  test_output       : last test / cmd output (overwritten, kept short)
  resolved          : issues that have been resolved
  next_steps        : pending work items
  lessons           : non-obvious insights (deduped, FIFO-capped)
  failed_approaches : approaches that did not work (bounded list)
  escalation_level  : per-node L0..L4 for Exception Handler (future)
  retry_counts      : per-node retry counter (managed by engine)
  iteration         : monotonic counter incremented each enrichment
"""

from camflow.engine.memory import MAX_LESSONS, add_lesson_deduped

MAX_COMPLETED = 20
MAX_FAILED_APPROACHES = 5
MAX_RESOLVED = 20
MAX_NEXT_STEPS = 10
MAX_TEST_HISTORY = 10
TEST_OUTPUT_MAX_CHARS = 3000


# ---- init helpers --------------------------------------------------------


def init_structured_fields(state):
    """Ensure the structured fields exist on `state`. Idempotent."""
    state.setdefault("iteration", 0)
    state.setdefault("active_task", None)
    state.setdefault("completed", [])
    state.setdefault("active_state", {})
    state.setdefault("blocked", None)
    state.setdefault("test_output", None)
    state.setdefault("test_history", [])
    state.setdefault("resolved", [])
    state.setdefault("next_steps", [])
    state.setdefault("lessons", [])
    state.setdefault("failed_approaches", [])
    state.setdefault("escalation_level", {})
    state.setdefault("retry_counts", {})
    state["active_state"].setdefault("key_files", [])
    state["active_state"].setdefault("modified_files", [])
    return state


def _summarize_test_output(text, iteration):
    """Produce a one-line summary of a pytest/cmd output block.

    Prefers the pytest summary line (e.g. "2 failed, 9 passed in 0.05s");
    falls back to the first non-empty line truncated to 80 chars.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    # Look for pytest-style summary on any line, preferring the last match
    for line in reversed(stripped.split("\n")):
        lower = line.lower()
        if " passed" in lower or " failed" in lower or " error" in lower:
            return f"iter {iteration}: {line.strip()[:120]}"
    # Fallback: first meaningful line
    for line in stripped.split("\n"):
        if line.strip():
            return f"iter {iteration}: {line.strip()[:80]}"
    return None


# ---- pruning -------------------------------------------------------------


def _prune_list(lst, cap):
    while len(lst) > cap:
        lst.pop(0)


def _dedup_list(lst):
    """Dedup a list of hashable items preserving order."""
    seen = set()
    out = []
    for item in lst:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _union_files(existing, incoming):
    """Union two lists of file paths, preserving order, deduped."""
    combined = list(existing) + [p for p in incoming if p not in existing]
    return combined


# ---- enrichment core -----------------------------------------------------


def _capture_files(state, state_updates):
    """Accept files the agent reports via state_updates.{files_touched,modified_files}.

    Returns the list of newly-touched files (for use in completed entries).
    """
    touched = (
        state_updates.get("files_touched")
        or state_updates.get("modified_files")
        or state_updates.get("key_files")
        or []
    )
    if isinstance(touched, str):
        touched = [touched]
    if not touched:
        return []
    active = state["active_state"]
    active["key_files"] = _union_files(active.get("key_files", []), touched)
    active["modified_files"] = _union_files(active.get("modified_files", []), touched)
    return touched


def _capture_test_output(state, node_result, cmd_output):
    """Decide whether to store stdout/stderr as test_output.

    Priority for the new content:
      1. explicit cmd_output argument (engine passes it for cmd nodes)
      2. node_result.output.stdout_tail on cmd failure (pytest, build)
      3. otherwise leave test_output untouched

    Observation-masking (§5.2): when we are about to overwrite
    test_output, archive the OLD value as a one-line entry in
    test_history first. Keeps long fix→test loops from bloating the
    prompt while preserving a trajectory summary the agent can see.
    """
    new_output = None
    if cmd_output is not None:
        new_output = cmd_output[-TEST_OUTPUT_MAX_CHARS:]
    else:
        output = node_result.get("output") or {}
        stdout_tail = output.get("stdout_tail")
        if stdout_tail and node_result.get("status") != "success":
            new_output = stdout_tail[-TEST_OUTPUT_MAX_CHARS:]

    if new_output is None:
        return

    prev = state.get("test_output")
    if prev:
        # Iteration was already incremented at the top of enrich_state;
        # attribute the summary to the current iteration step.
        summary = _summarize_test_output(prev, state.get("iteration", 0))
        if summary:
            history = state.setdefault("test_history", [])
            history.append(summary)
            _prune_list(history, MAX_TEST_HISTORY)

    state["test_output"] = new_output


def _record_success(state, node_id, node_result, touched_files):
    """Append to completed, update resolved, clear blocked, purge matching failed_approaches."""
    summary = node_result.get("summary") or ""
    output = node_result.get("output") or {}
    state_updates = node_result.get("state_updates") or {}

    entry = {
        "node": node_id,
        "action": summary,
    }
    detail = state_updates.get("detail") or output.get("detail")
    if detail:
        entry["detail"] = detail
    if touched_files:
        entry["file"] = touched_files[0] if len(touched_files) == 1 else ", ".join(touched_files)
    lines = state_updates.get("lines") or output.get("lines")
    if lines:
        entry["lines"] = lines

    completed = state["completed"]
    completed.append(entry)
    _prune_list(completed, MAX_COMPLETED)

    resolved = state_updates.get("resolved")
    if resolved:
        if isinstance(resolved, str):
            resolved = [resolved]
        state["resolved"].extend(resolved)
        state["resolved"] = _dedup_list(state["resolved"])
        _prune_list(state["resolved"], MAX_RESOLVED)

    # Success clears the current obstacle
    state["blocked"] = None

    # Drop matching failed_approaches entries for this node
    state["failed_approaches"] = [
        fa for fa in state["failed_approaches"] if fa.get("node") != node_id
    ]


def _record_failure(state, node_id, node_result):
    """Set blocked, append failed_approaches."""
    summary = node_result.get("summary") or ""
    error = node_result.get("error") or {}
    state["blocked"] = {
        "node": node_id,
        "reason": summary,
        "error_code": error.get("code") if isinstance(error, dict) else None,
    }
    failed = state["failed_approaches"]
    failed.append({
        "node": node_id,
        "approach": summary,
        "iteration": state["iteration"],
    })
    _prune_list(failed, MAX_FAILED_APPROACHES)


def _update_next_steps(state, state_updates):
    next_steps = state_updates.get("next_steps")
    if not next_steps:
        return
    if isinstance(next_steps, str):
        next_steps = [next_steps]
    if isinstance(next_steps, list):
        state["next_steps"] = _dedup_list(list(next_steps))[:MAX_NEXT_STEPS]


def _update_active_task(state, state_updates, node_id):
    """Prefer explicit state_updates.active_task; otherwise keep existing."""
    at = state_updates.get("active_task")
    if at:
        state["active_task"] = at
    elif state.get("active_task") is None:
        state["active_task"] = f"executing node '{node_id}'"


# ---- public API ----------------------------------------------------------


def enrich_state(state, node_id, node_result, cmd_output=None):
    """Merge a single node execution result into the structured state.

    Args:
        state: the workflow state dict (mutated in place)
        node_id: the node that just ran
        node_result: the dict returned by node_runner / cmd_runner / agent_runner
                     (must have at least 'status'; everything else optional)
        cmd_output: optional explicit stdout from a cmd node, to capture as
                    test_output

    Returns:
        The same state dict, enriched.

    Effects:
        - iteration++
        - lessons: append (with dedup + FIFO prune) any new_lesson
        - key_files / modified_files: union with any touched files
        - test_output: overwritten if cmd_output or fail-with-stdout_tail
        - on success: append to completed, extend resolved, clear blocked,
                      drop matching failed_approaches
        - on fail: set blocked, append failed_approaches
        - next_steps: replaced if state_updates.next_steps provided
        - active_task: set if state_updates.active_task provided
    """
    init_structured_fields(state)
    state["iteration"] = state.get("iteration", 0) + 1

    status = node_result.get("status", "fail")
    state_updates = dict(node_result.get("state_updates") or {})

    # Handoff: a detailed paragraph from the finishing agent to the next
    # agent. Only the most recent handoff is retained — this is a
    # high-signal, low-volume channel for "what I tried + exact file/line
    # + what to try next", not a log. Missing field is fine (old agents
    # without handoff still work).
    handoff = (node_result.get("handoff") or "").strip()
    if handoff:
        state["last_handoff"] = handoff

    # Lessons (dedup + prune)
    new_lesson = state_updates.pop("new_lesson", None)
    if new_lesson:
        add_lesson_deduped(state["lessons"], new_lesson, max_lessons=MAX_LESSONS)

    touched = _capture_files(state, state_updates)
    _capture_test_output(state, node_result, cmd_output)
    _update_next_steps(state, state_updates)
    _update_active_task(state, state_updates, node_id)

    if status == "success":
        _record_success(state, node_id, node_result, touched)
    else:
        _record_failure(state, node_id, node_result)

    return state
