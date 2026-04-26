"""Orphan agent handling on engine resume.

When the engine crashes mid-node, the agent may still be running in its
tmux session. Naive resume would start a second agent for the same node.

State carries `current_agent_id` whenever an agent is active. On resume we
look at that ID and decide how to handle it.
"""

import os

from camflow.backend.cam.agent_runner import (
    _get_agent_status,
    _wait_for_result,
    finalize_agent,
    RESULT_FILE,
)
from camflow.registry import get_agent, on_agent_finalized


# Classification returned by decide_orphan_action
ACTION_NO_ORPHAN = "no_orphan"
ACTION_WAIT = "wait_for_orphan"
ACTION_ADOPT_RESULT = "adopt_result"
ACTION_TREAT_AS_CRASH = "treat_as_crash"


def decide_orphan_action(state, project_dir):
    """Examine state + filesystem + camc registry and decide what to do.

    Returns:
        (action, agent_id)
        action is one of the ACTION_* constants above.
    """
    agent_id = state.get("current_agent_id")
    if not agent_id:
        return (ACTION_NO_ORPHAN, None)

    status_info = _get_agent_status(agent_id)
    result_path = os.path.join(project_dir, RESULT_FILE)
    result_exists = os.path.exists(result_path)

    if status_info is None:
        # Agent not in registry. Did it finish and write the result before vanishing?
        if result_exists:
            return (ACTION_ADOPT_RESULT, agent_id)
        return (ACTION_TREAT_AS_CRASH, agent_id)

    status = status_info.get("status")

    if status == "running":
        # Still alive. Wait for it (don't start a duplicate).
        return (ACTION_WAIT, agent_id)

    if status in ("completed", "stopped"):
        if result_exists:
            return (ACTION_ADOPT_RESULT, agent_id)
        # Completed but no result file — treat as crash so we retry or fail cleanly
        return (ACTION_TREAT_AS_CRASH, agent_id)

    if status == "failed":
        return (ACTION_TREAT_AS_CRASH, agent_id)

    # Unknown status — be safe, treat as crash
    return (ACTION_TREAT_AS_CRASH, agent_id)


def _update_registry_if_present(project_dir, agent_id, result, completion_signal):
    """Best-effort: flip an orphaned agent's registry status to match
    the synthesized / read result. If the agent isn't in the registry
    (engine crashed before registering it), do nothing — the registry
    is observability, not correctness.
    """
    try:
        if get_agent(project_dir, agent_id) is None:
            return
        on_agent_finalized(
            project_dir,
            agent_id=agent_id,
            result=result,
            flow_id=None,  # orphan path doesn't carry flow_id context
            completion_signal=completion_signal,
        )
    except Exception:
        # Recovery path — never raise; the resume must continue.
        pass


def handle_orphan(action, agent_id, project_dir, timeout, poll_interval):
    """Execute the decided action and return a node result.

    Returns:
        (result_dict, completion_signal)
        - for ACTION_ADOPT_RESULT: read result file, signal = "adopted_result"
        - for ACTION_WAIT: resume polling, signal = what completion returned
        - for ACTION_TREAT_AS_CRASH: synthesize a fail result, signal = "adopted_crash"
        - for ACTION_NO_ORPHAN: raises (should not be called)
    """
    if action == ACTION_NO_ORPHAN:
        raise ValueError("handle_orphan called with no orphan to handle")

    result_path = os.path.join(project_dir, RESULT_FILE)

    if action == ACTION_WAIT:
        completion_signal, _ = _wait_for_result(
            agent_id, result_path, timeout, poll_interval
        )
        result = finalize_agent(agent_id, completion_signal, project_dir)
        _update_registry_if_present(project_dir, agent_id, result, completion_signal)
        return (result, completion_signal)

    if action == ACTION_ADOPT_RESULT:
        # Result file already exists; read it, clean up agent, return
        result = finalize_agent(agent_id, "file_appeared", project_dir)
        _update_registry_if_present(project_dir, agent_id, result, "adopted_result")
        return (result, "adopted_result")

    if action == ACTION_TREAT_AS_CRASH:
        # Synthesize a crash result so engine can retry
        result = {
            "status": "fail",
            "summary": "agent crashed before engine resumed; no result file",
            "state_updates": {},
            "error": {"code": "AGENT_CRASH", "reason": "orphan detected on resume"},
        }
        # Clean up stale agent if it's still lingering
        from camflow.backend.cam.agent_runner import _cleanup_agent
        _cleanup_agent(agent_id)
        _update_registry_if_present(project_dir, agent_id, result, "adopted_crash")
        return (result, "adopted_crash")

    raise ValueError(f"unknown action: {action}")
