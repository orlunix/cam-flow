"""`camflow status` subcommand — report engine liveness + progress.

Reads ``.camflow/heartbeat.json`` and ``.camflow/state.json`` (without
touching either) and prints a human-readable summary. Distinguishes
three cases:

  * ALIVE   — heartbeat fresh, pid exists → engine is running now
  * DEAD    — heartbeat stale and pid missing → engine crashed
  * IDLE    — no heartbeat at all → engine never ran, or cleanly exited

Workflow argument is optional. If not supplied, we look at
``.camflow/heartbeat.json`` in the project directory (default: cwd)
and pull the ``workflow_path`` the engine wrote there. That way the
user can run ``camflow status`` from the project root without having
to remember which yaml they started.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from camflow.backend.persistence import load_state
from camflow.engine.dsl import load_workflow
from camflow.engine.monitor import (
    DEFAULT_STALE_THRESHOLD,
    _parse_iso,
    heartbeat_path,
    is_process_alive,
    is_stale,
    load_heartbeat,
)
from camflow.engine.watchdog import watchdog_pid_path


# ---- formatting helpers --------------------------------------------------


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(seconds)
    if s < 0:
        return "0s"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _fmt_age(timestamp_iso: str | None) -> tuple[str, int | None]:
    """Return (human-readable age, age_in_seconds) of an ISO timestamp."""
    ts = _parse_iso(timestamp_iso or "")
    if ts is None:
        return ("unknown", None)
    age = int(time.time() - ts)
    return (_fmt_duration(age) + " ago", age)


def _read_watchdog_pid(project_dir: str) -> int | None:
    """Return the watchdog's pid from ``.camflow/watchdog.pid``, or None."""
    try:
        with open(watchdog_pid_path(project_dir), "r", encoding="utf-8") as f:
            content = f.read().strip()
        return int(content) if content else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _count_completed(state: dict) -> int:
    completed = state.get("completed") or []
    if isinstance(completed, list):
        return len(completed)
    return 0


def _completed_node_ids(state: dict) -> set[str]:
    """Pull the set of completed node_ids out of state.completed.

    state.completed is a list of dicts with at least a ``node`` key;
    we also accept bare strings for robustness.
    """
    out: set[str] = set()
    for entry in state.get("completed") or []:
        if isinstance(entry, dict):
            node = entry.get("node")
            if isinstance(node, str):
                out.add(node)
        elif isinstance(entry, str):
            out.add(entry)
    return out


def _progress_bars(workflow: dict, state: dict, liveness: str) -> list[str]:
    """Render one line per node in declaration order::

        [done] eval_vexriscv
        [>>>>] eval_swerv
        [    ] eval_c910

    The current pc shows the in-progress marker when the engine is
    ALIVE, a crash marker (``[X]``) when DEAD, and pending otherwise.
    """
    if not workflow:
        return []
    completed = _completed_node_ids(state)
    current = state.get("pc")
    lines = []
    for node_id in workflow:
        if node_id in completed:
            marker = "[done]"
        elif node_id == current:
            if liveness == "ALIVE":
                marker = "[>>>>]"
            elif liveness == "DEAD":
                marker = "[XXXX]"
            else:
                marker = "[----]"
        else:
            marker = "[    ]"
        lines.append(f"  {marker} {node_id}")
    return lines


# ---- workflow discovery --------------------------------------------------


def _discover_workflow(workflow_arg: str | None, project_dir: str) -> str | None:
    """Resolve the workflow path.

    Explicit arg wins. Otherwise read heartbeat.json or state.json for
    a stored ``workflow_path``. Returns None if we cannot figure it out;
    status still works in that case (we just won't know node order).
    """
    if workflow_arg:
        return workflow_arg
    hb = load_heartbeat(heartbeat_path(project_dir)) or {}
    if hb.get("workflow_path"):
        return hb["workflow_path"]
    # state.json doesn't carry workflow_path today; left here for future.
    return None


# ---- main command --------------------------------------------------------


def status_command(args) -> int:
    """Implementation of the subcommand. Returns a shell exit code.

    0 → engine ALIVE
    1 → engine DEAD (crashed) or workflow is in a terminal-error state
    2 → nothing to report (no state at all)
    """
    project_dir = args.project_dir or os.getcwd()
    workflow_path = _discover_workflow(args.workflow, project_dir)

    # If the workflow arg is what resolved the project dir (old behavior),
    # fall back to the workflow's own directory when no explicit project_dir.
    if args.workflow and not args.project_dir:
        project_dir = os.path.dirname(os.path.abspath(args.workflow)) or "."
        workflow_path = args.workflow

    workflow = None
    if workflow_path:
        if not os.path.isfile(workflow_path):
            print(
                f"ERROR: workflow file not found: {workflow_path}",
                file=sys.stderr,
            )
            return 1
        try:
            workflow = load_workflow(workflow_path)
        except Exception as e:
            print(f"ERROR: failed to load workflow: {e}", file=sys.stderr)
            return 1

    state_path = os.path.join(project_dir, ".camflow", "state.json")
    state = load_state(state_path) or {}
    if not state:
        print(f"Workflow: {workflow_path or '(unknown)'}")
        print("State:    none (workflow has not been run)")
        return 2

    heartbeat = load_heartbeat(heartbeat_path(project_dir))
    pid = heartbeat.get("pid") if heartbeat else None
    heartbeat_age_str, _ = _fmt_age(
        heartbeat.get("timestamp") if heartbeat else None
    )

    # Liveness classification:
    #   * heartbeat missing → IDLE (never ran, or cleanly exited)
    #   * heartbeat fresh AND pid alive → ALIVE
    #   * else → DEAD (crashed)
    if heartbeat is None:
        liveness = "IDLE"
    elif not is_stale(heartbeat) and is_process_alive(pid):
        liveness = "ALIVE"
    else:
        liveness = "DEAD"

    workflow_status = state.get("status")
    pc = state.get("pc")
    completed_count = _count_completed(state)
    total_nodes = len(workflow) if workflow else 0

    print(f"Workflow: {workflow_path or '(unknown)'}")

    if liveness == "ALIVE":
        print(f"Engine:   ALIVE (pid {pid}, heartbeat {heartbeat_age_str})")
    elif liveness == "DEAD":
        alive_str = "not running" if not is_process_alive(pid) else "still present"
        print(
            f"Engine:   DEAD (last heartbeat {heartbeat_age_str}, "
            f"pid {pid} {alive_str})"
        )
    else:
        print(f"Engine:   IDLE (no active heartbeat; workflow status={workflow_status!r})")

    # Watchdog line — either ALIVE (pid x), DEAD (stale pidfile), or OFF.
    wd_pid = _read_watchdog_pid(project_dir)
    if wd_pid is None:
        print("Watchdog: OFF (no .camflow/watchdog.pid)")
    elif is_process_alive(wd_pid):
        print(f"Watchdog: ALIVE (pid {wd_pid})")
    else:
        print(f"Watchdog: DEAD (pid {wd_pid} not running — stale pidfile)")

    # Node line — carries iteration + attempt so the user can see if
    # the engine is stuck re-running the same node.
    iteration = (heartbeat or {}).get("iteration")
    if iteration is None:
        iteration = state.get("iteration")
    retry_counts = state.get("retry_counts") or {}
    attempt = (retry_counts.get(pc) or 0) + 1
    node_suffix_parts = []
    if iteration is not None:
        node_suffix_parts.append(f"iteration {iteration}")
    node_suffix_parts.append(f"attempt {attempt}")
    node_suffix = " (" + ", ".join(node_suffix_parts) + ")"
    if liveness == "DEAD":
        print(f"Node:     {pc}{node_suffix} — was in progress")
    else:
        print(f"Node:     {pc}{node_suffix}")

    # Agent line — include running duration when we know it.
    agent_id = (heartbeat or {}).get("agent_id") or state.get("current_agent_id")
    if agent_id:
        agent_started = (heartbeat or {}).get("agent_started_at") or state.get(
            "current_node_started_at"
        )
        duration_str = ""
        if agent_started:
            try:
                duration_str = f", {_fmt_duration(time.time() - float(agent_started))}"
            except (TypeError, ValueError):
                duration_str = ""
        if liveness == "ALIVE":
            print(f"Agent:    {agent_id} (running{duration_str})")
        else:
            print(f"Agent:    {agent_id} (orphan — will be reaped on resume)")

    # Progress block — bracket visualization for each node.
    print(f"Progress: {completed_count}/{total_nodes} nodes completed")
    for line in _progress_bars(workflow or {}, state, liveness):
        print(line)

    # Uptime comes from the heartbeat's engine-start timestamp.
    uptime = (heartbeat or {}).get("uptime_seconds")
    if uptime is not None:
        print(f"Uptime:   {_fmt_duration(uptime)}")

    if liveness == "DEAD":
        wf_arg = workflow_path or "<workflow.yaml>"
        print(
            f"Recovery: run `camflow resume {wf_arg}` to continue from {pc!r}"
        )
        return 1

    if workflow_status in ("failed", "engine_error", "aborted", "interrupted"):
        wf_arg = workflow_path or "<workflow.yaml>"
        print(
            f"Recovery: run `camflow resume {wf_arg}` to continue from "
            f"{pc!r} (prev status: {workflow_status})"
        )
        return 1

    return 0


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow status")
        p = parser
    else:
        p = subparsers.add_parser(
            "status",
            help="Report engine liveness + workflow progress",
        )
    # Workflow is now optional — the engine writes workflow_path into
    # heartbeat.json so `camflow status` without args still works.
    p.add_argument(
        "workflow", nargs="?", default=None,
        help="Path to workflow YAML file (optional; read from heartbeat "
             "if omitted)",
    )
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd, or the workflow's directory "
             "if workflow is given)",
    )
    p.set_defaults(func=status_command)
    if subparsers is None:
        return parser
    return p


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
