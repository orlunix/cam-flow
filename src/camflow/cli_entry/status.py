"""`camflow status` subcommand — report engine liveness + progress.

Reads ``.camflow/heartbeat.json`` and ``.camflow/state.json`` (without
touching either) and prints a human-readable summary. Distinguishes
three cases:

  * ALIVE   — heartbeat fresh, pid exists → engine is running now
  * DEAD    — heartbeat stale and pid missing → engine crashed
  * IDLE    — no heartbeat at all → engine never ran, or cleanly exited
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


def _count_completed(state: dict) -> int:
    completed = state.get("completed") or []
    if isinstance(completed, list):
        return len(completed)
    return 0


def status_command(args) -> int:
    """Implementation of the subcommand. Returns a shell exit code.

    0 → engine ALIVE
    1 → engine DEAD (crashed) or workflow is in a terminal-error state
    2 → nothing to report (no state at all)
    """
    workflow_path = args.workflow
    project_dir = (
        args.project_dir
        or os.path.dirname(os.path.abspath(workflow_path))
        or "."
    )

    if not os.path.isfile(workflow_path):
        print(f"ERROR: workflow file not found: {workflow_path}", file=sys.stderr)
        return 1

    try:
        workflow = load_workflow(workflow_path)
    except Exception as e:
        print(f"ERROR: failed to load workflow: {e}", file=sys.stderr)
        return 1

    state_path = os.path.join(project_dir, ".camflow", "state.json")
    state = load_state(state_path) or {}
    if not state:
        print(f"Workflow: {workflow_path}")
        print("State:    none (workflow has not been run)")
        return 2

    heartbeat = load_heartbeat(heartbeat_path(project_dir))
    pid = heartbeat.get("pid") if heartbeat else None
    heartbeat_age_str, heartbeat_age_s = _fmt_age(
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

    print(f"Workflow: {workflow_path}")
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

    iter_hint = ""
    if heartbeat and heartbeat.get("iteration") is not None:
        iter_hint = f" (iteration {heartbeat['iteration']})"
    if liveness == "DEAD":
        print(f"Node:     {pc} (was in progress){iter_hint}")
    else:
        print(f"Node:     {pc}{iter_hint}")

    agent_id = (heartbeat or {}).get("agent_id") or state.get("current_agent_id")
    if agent_id:
        agent_state = "running" if liveness == "ALIVE" else "orphan (will be reaped on resume)"
        print(f"Agent:    {agent_id} ({agent_state})")

    print(f"Completed: {completed_count}/{total_nodes} nodes")
    completed = state.get("completed") or []
    # Show up to 10 recent completions so the line doesn't explode on
    # long workflows.
    for entry in completed[-10:]:
        if not isinstance(entry, dict):
            continue
        node = entry.get("node", "?")
        action = entry.get("action", "")
        action_suffix = f": {action}" if action else ""
        print(f"  - {node}{action_suffix}")

    uptime = (heartbeat or {}).get("uptime_seconds")
    if uptime is not None:
        print(f"Uptime:   {_fmt_duration(uptime)}")

    if liveness == "DEAD":
        print(
            f"Recovery: run `camflow run {workflow_path}` to auto-resume "
            f"from {pc!r}"
        )
        return 1

    if workflow_status in ("failed", "engine_error", "aborted", "interrupted"):
        # Engine isn't holding a heartbeat (clean exit), but the workflow
        # ended in a resumable failure — same recovery path.
        print(
            f"Recovery: run `camflow run {workflow_path}` to auto-resume "
            f"from {pc!r} (prev status: {workflow_status})"
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
    p.add_argument("workflow", help="Path to workflow YAML file")
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: directory of the workflow file)",
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
