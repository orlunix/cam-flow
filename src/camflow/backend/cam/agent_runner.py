"""Agent node runner — executes nodes via camc.

Completion is signaled by the result file appearing on disk. We do
NOT trust camc's idle/auto-exit detection (known unreliable, bug #10):
agents do the work but never voluntarily exit. Instead the engine
owns the agent lifecycle:

  1. Clear `.camflow/node-result.json` (stale safety)
  2. Write the full prompt to `.camflow/node-prompt.txt`
     (long multi-line prompts corrupt tmux paste)
  3. `camc run --name camflow-<node> --path <dir>` WITHOUT --auto-exit;
     the prompt arg is a short instruction telling the agent to read
     the prompt file
  4. Send Enter once the TUI is ready (camc pastes but does not submit)
  5. Poll for result file appearance (PRIMARY signal). camc status is
     consulted only as a SECONDARY hint to detect the rare case where
     the agent process actually died.
  6. On result file present OR timeout OR agent gone: explicit
     `camc stop` (graceful) then `camc rm --force` (cleanup). The
     engine kills the agent — never relies on auto-exit.
"""

import json
import os
import re
import subprocess
import time

from camflow.backend.cam.result_reader import clear_node_result, read_node_result


PROMPT_FILE = ".camflow/node-prompt.txt"
RESULT_FILE = ".camflow/node-result.json"
CAPTURE_LINES = 50


# ---- subprocess helpers --------------------------------------------------


def _parse_agent_id(output):
    m = re.search(r"agent\s+([0-9a-f]{6,12})", output)
    if m:
        return m.group(1)
    m = re.search(r"ID:\s+([0-9a-f]{6,12})", output)
    if m:
        return m.group(1)
    return None


def _get_agent_status(agent_id):
    """Return status dict via `camc --json status <id>`, or None if not found."""
    try:
        proc = subprocess.run(
            ["camc", "--json", "status", agent_id],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def _capture_screen(agent_id, lines=CAPTURE_LINES):
    """Grab last N lines of agent screen via `camc capture`."""
    try:
        proc = subprocess.run(
            ["camc", "capture", agent_id, "-n", str(lines)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return ""


def _stop_agent(agent_id):
    """Graceful stop: send the agent an exit request via tmux."""
    try:
        subprocess.run(
            ["camc", "stop", agent_id],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _rm_agent(agent_id):
    """Hard remove from camc registry. `--kill` also kills the tmux session."""
    try:
        subprocess.run(
            ["camc", "rm", agent_id, "--kill"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _cleanup_agent(agent_id):
    """Engine owns the agent lifecycle: stop then rm. Always called."""
    _stop_agent(agent_id)
    # brief grace period for stop to take effect before force rm
    time.sleep(1)
    _rm_agent(agent_id)


def _send_key(agent_id, key):
    try:
        subprocess.run(
            ["camc", "key", agent_id, "--key", key],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _kick_prompt(agent_id, max_wait=30, poll=1):
    """Submit the queued prompt by sending Enter once the TUI is ready.

    `camc run "<prompt>"` pastes the prompt into the Claude Code TUI input
    box but does NOT submit it. Without this kick the agent sits at the
    prompt forever.

    Strategy:
      1. Poll the screen until we see the prompt char (❯ / > / ›) on the
         last 5 lines AND text after it.
      2. Send Enter once to submit.
      3. Fallback: send Enter anyway after max_wait seconds.

    Idempotent: extra Enter is harmless if the agent moved past the prompt.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll)
        screen = _capture_screen(agent_id, lines=20)
        if not screen:
            continue
        last_lines = [ln for ln in screen.strip().split("\n")[-5:] if ln.strip()]
        for ln in last_lines:
            stripped = ln.lstrip()
            if not stripped:
                continue
            if stripped[0] in ("❯", ">", "›") and len(stripped) > 2:
                _send_key(agent_id, "Enter")
                return
    _send_key(agent_id, "Enter")  # fallback


# ---- core polling --------------------------------------------------------


def _wait_for_result(agent_id, result_path, timeout, poll_interval):
    """Wait for the agent's result file to appear.

    PRIMARY signal: `.camflow/node-result.json` exists on disk.
    SECONDARY signal: `camc status` reports the agent is gone from the
                      registry — this means the process died unexpectedly
                      (we never use --auto-exit, so the agent should
                      never voluntarily disappear during normal work).

    We do NOT trust `state == idle` (camc bug #10) or
    `status in {completed, stopped, failed}` for completion — we own the
    lifecycle and only mark these as terminal when WE stop the agent.

    Returns:
        (reason, detail) where reason is one of:
          "file_appeared" — result file exists on disk
          "agent_gone"    — camc lost track of the agent (process died)
          "timeout"       — neither happened within `timeout` seconds
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)

        # PRIMARY: result file
        if os.path.exists(result_path):
            time.sleep(1)  # grace for fsync
            return ("file_appeared", None)

        # SECONDARY: did the agent process disappear?
        status_info = _get_agent_status(agent_id)
        if status_info is None:
            return ("agent_gone", None)
        # Otherwise keep polling — even "idle" or "stopped" without a result
        # file means we should keep waiting. The agent might still be
        # working. Only the file is authoritative.

    return ("timeout", None)


# ---- public API ----------------------------------------------------------


def start_agent(node_id, prompt, project_dir, allowed_tools=None):
    """Start a camc agent for a node.

    Writes the prompt to `.camflow/node-prompt.txt` (because tmux paste
    corrupts long multiline prompts), then runs camc with a short
    instruction telling the agent to read the file.

    Notably: NO --auto-exit flag. The engine owns shutdown via
    cleanup_agent once the result file appears.

    `allowed_tools`: list of Claude Code tool names to restrict the
    agent to (§5.3 HQ.3). Current camc releases do NOT expose
    `--allowed-tools` on the `run` subcommand, so this parameter is
    accepted for API compatibility and enforced as a SOFT constraint
    at the prompt level (prompt_builder renders a "Tools you may use"
    line when the node's workflow.yaml sets `allowed_tools`). When
    camc gains `--allowed-tools` we'll switch to hard enforcement
    here without an API change.

    Returns:
        agent_id (str). Raises RuntimeError on launch failure.
    """
    camflow_dir = os.path.join(project_dir, ".camflow")
    os.makedirs(camflow_dir, exist_ok=True)

    clear_node_result(project_dir)

    prompt_path = os.path.join(camflow_dir, "node-prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    short_prompt = (
        "Read the file .camflow/node-prompt.txt and follow ALL instructions "
        "inside it exactly. The file contains your complete task and output "
        "requirements."
    )

    try:
        proc = subprocess.run(
            [
                "camc", "run",
                "--name", f"camflow-{node_id}",
                "--path", project_dir,
                # NO --auto-exit: engine owns the lifecycle
                short_prompt,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"camc run failed to launch: {e}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"camc run returned {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    agent_id = _parse_agent_id(proc.stdout)
    if not agent_id:
        raise RuntimeError(
            f"could not parse agent ID from camc output: {proc.stdout[:500]}"
        )

    # camc pastes the prompt into the TUI input box but doesn't submit it;
    # send Enter once the TUI is ready so the agent actually starts working.
    _kick_prompt(agent_id)

    return agent_id


def finalize_agent(agent_id, completion_signal, project_dir, cleanup=True):
    """Read the node result, capture screen on failure, and stop+rm the agent.

    The engine ALWAYS owns shutdown — we stop and rm the agent here
    regardless of how completion was reached (file_appeared / agent_gone /
    timeout). Without --auto-exit the agent would sit forever otherwise.
    """
    result_path = os.path.join(project_dir, RESULT_FILE)
    result = read_node_result(project_dir)

    # If result file missing, classify the failure based on completion_signal
    if result.get("status") == "fail" and "did not write result" in (result.get("summary") or ""):
        if completion_signal == "timeout":
            capture = _capture_screen(agent_id)
            result = {
                "status": "fail",
                "summary": "agent timed out, no result file",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "AGENT_TIMEOUT", "completion_signal": completion_signal},
            }
        elif completion_signal == "agent_gone":
            capture = _capture_screen(agent_id)  # likely empty
            result = {
                "status": "fail",
                "summary": "agent process died before writing result",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "AGENT_CRASH", "completion_signal": completion_signal},
            }
        # No status_terminal/status_idle_stable branches anymore — those
        # signals are not produced by _wait_for_result.

    if cleanup:
        _cleanup_agent(agent_id)

    return result


def run_agent(node_id, prompt, project_dir, timeout=600, poll_interval=5):
    """Execute an agent node end-to-end.

    Returns:
        (result_dict, agent_id, completion_signal)
        completion_signal ∈ {file_appeared, agent_gone, timeout, launch_failed}
    """
    try:
        agent_id = start_agent(node_id, prompt, project_dir)
    except RuntimeError as e:
        return (
            {
                "status": "fail",
                "summary": f"failed to launch agent: {e}",
                "state_updates": {},
                "error": {"code": "CAMC_ERROR", "message": str(e)},
            },
            None,
            "launch_failed",
        )

    result_path = os.path.join(project_dir, RESULT_FILE)
    completion_signal, _detail = _wait_for_result(
        agent_id, result_path, timeout, poll_interval
    )
    result = finalize_agent(agent_id, completion_signal, project_dir)
    return (result, agent_id, completion_signal)
