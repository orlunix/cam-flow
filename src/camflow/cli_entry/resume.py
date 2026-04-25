"""`camflow resume` subcommand — pick up a stopped or failed workflow.

The engine already resumes automatically when state.json shows
`status=running` — it loads pc from disk and continues. This
subcommand handles the cases the auto-resume path doesn't:

  * The workflow ended in `failed` (or `engine_error`, `aborted`):
    flip status back to `running` so the engine will execute the
    same node again. Optionally jump to a different node first
    (`--from`).
  * The workflow ended in `done` and the user wants to re-run from
    a specific node: `--from <node>` is required (otherwise we
    refuse — silently rerunning a completed workflow is a footgun).
  * The workflow is still `running` (engine crashed without writing
    a terminal status): leave status as-is and just hand off to the
    engine — same as `camflow <workflow.yaml>`.

What this subcommand does NOT do:
  - Reset `completed`, `lessons`, `failed_approaches`, trace.log,
    or any other accumulated state. Resume is a continuation, not
    a fresh start. (`camflow <workflow.yaml> --force-restart` is
    the closest thing to a clean reset.)
  - Mutate the workflow YAML.

Examples:

    camflow resume workflow.yaml
        # take state.json as-is, run the engine; if status is
        # failed/aborted, flip to running and retry the failed node.

    camflow resume workflow.yaml --from validate_trace
        # jump pc to validate_trace, then continue.

    camflow resume workflow.yaml --retry
        # explicit form of "flip a non-running status to running"
        # without changing pc. Useful when status was 'done' (rerun
        # the last node) or 'aborted' (continue from where you left).
"""

from __future__ import annotations

import argparse
import os
import sys

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.backend.persistence import load_state, save_state_atomic
from camflow.cli_entry.daemon import daemonize_engine, spawn_watchdog
from camflow.engine.dsl import load_workflow


# Statuses we'll auto-flip to "running" when the user runs `resume`.
# `done` is intentionally NOT in this set — re-running a completed
# workflow without an explicit --from would silently re-execute every
# node, which is almost never what the user meant.
RESUMABLE_FAILED_STATUSES = {"failed", "aborted", "engine_error"}


def _resolve_state_path(workflow_path: str, project_dir: str | None) -> str:
    project_dir = project_dir or os.path.dirname(os.path.abspath(workflow_path)) or "."
    return os.path.join(project_dir, ".camflow", "state.json")


def _prepare_state(state: dict, workflow: dict, *,
                   from_node: str | None, retry: bool) -> tuple[dict, list[str]]:
    """Apply --from / --retry to a loaded state in place.

    Returns (state, [actions_taken]) so the CLI can report what changed.
    Raises ValueError on user input that doesn't make sense (--from to
    a node that doesn't exist; resuming `done` without --from).
    """
    actions: list[str] = []
    status = state.get("status", "running")

    if from_node is not None:
        if from_node not in workflow:
            raise ValueError(
                f"--from target {from_node!r} is not a node in the workflow. "
                f"Known nodes: {', '.join(sorted(workflow.keys()))[:200]}"
            )
        if state.get("pc") != from_node:
            actions.append(f"pc {state.get('pc')!r} → {from_node!r}")
        state["pc"] = from_node
        # Clear any blocked/last-failure metadata tied to the previous
        # pc — the user is choosing where to start, not retrying the
        # same broken node.
        if state.get("blocked"):
            state["blocked"] = None
            actions.append("cleared state.blocked")
        if state.get("last_failure"):
            state.pop("last_failure", None)
            actions.append("cleared state.last_failure")

    # Decide whether to flip status to running.
    flip_to_running = False
    if retry:
        if status != "running":
            flip_to_running = True
        else:
            # --retry on an already-running state is a no-op; warn so
            # the user knows nothing happened.
            actions.append(f"status already 'running' — --retry is a no-op")
    elif status in RESUMABLE_FAILED_STATUSES:
        flip_to_running = True
    elif status == "done":
        if from_node is None:
            raise ValueError(
                "workflow status is 'done'. Pass --from <node_id> to "
                "explicitly re-run from a specific node, or use "
                "`camflow <workflow.yaml> --force-restart` for a clean "
                "restart."
            )
        flip_to_running = True
    elif status == "running":
        # Auto-resume case — leave it alone, engine will continue.
        pass
    elif status == "waiting":
        # Don't auto-flip — the workflow is waiting on something. The
        # user must opt in via --retry.
        if not retry:
            raise ValueError(
                "workflow status is 'waiting' (external event). Use "
                "--retry if you want to force it back to running."
            )

    if flip_to_running:
        actions.append(f"status {status!r} → 'running'")
        state["status"] = "running"
        # Reset retry_counts for the current pc so the resumed node
        # gets a fresh budget. Without this, a node that already
        # exhausted its retries on the previous run would refuse to
        # try again. Other nodes' counts are preserved.
        retry_counts = state.get("retry_counts") or {}
        if state["pc"] in retry_counts:
            retry_counts[state["pc"]] = 0
            actions.append(f"reset retry_counts[{state['pc']!r}] to 0")
        state["retry_counts"] = retry_counts
        # Same idea for the per-node execution counter — a stuck node
        # that hit the loop guard last time deserves a fresh budget on
        # explicit resume.
        node_exec = state.get("node_execution_count") or {}
        if state["pc"] in node_exec:
            node_exec[state["pc"]] = 0
            actions.append(f"reset node_execution_count[{state['pc']!r}] to 0")
        state["node_execution_count"] = node_exec
        # Clear the terminal error (if any) so the engine doesn't think
        # it's still in a failed state.
        state.pop("error", None)

    return state, actions


def resume_command(args) -> int:
    if not os.path.isfile(args.workflow):
        print(f"ERROR: workflow file not found: {args.workflow}", file=sys.stderr)
        return 1

    workflow = load_workflow(args.workflow)
    project_dir = args.project_dir or os.path.dirname(os.path.abspath(args.workflow)) or "."
    state_path = _resolve_state_path(args.workflow, args.project_dir)

    state = load_state(state_path)
    if state is None:
        print(
            f"ERROR: no state.json at {state_path}. Use "
            f"`camflow {args.workflow}` for a fresh run.",
            file=sys.stderr,
        )
        return 1

    try:
        state, actions = _prepare_state(
            state, workflow,
            from_node=args.from_node, retry=args.retry,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if actions:
        print("[resume] applied:", file=sys.stderr)
        for a in actions:
            print(f"  - {a}", file=sys.stderr)
    else:
        print("[resume] no state changes; handing off to engine", file=sys.stderr)

    # Persist the resume edits BEFORE handing off — the engine reads
    # state.json at startup, so it must reflect our changes already.
    save_state_atomic(state_path, state)

    if args.dry_run:
        print(
            f"[resume] --dry-run: would resume from pc={state.get('pc')!r} "
            f"with status={state.get('status')!r}",
            file=sys.stderr,
        )
        return 0

    if getattr(args, "daemon", False):
        parent = daemonize_engine(project_dir)
        if parent:
            # Spawn watchdog for the resumed engine too. If a watchdog is
            # already running (common case: watchdog itself invoked this
            # resume), the new one will fail to acquire the lock and
            # exit harmlessly.
            if not getattr(args, "no_watchdog", False):
                spawn_watchdog(
                    args.workflow, project_dir,
                    max_restarts=getattr(args, "watchdog_max_restarts", None),
                )
            return 0
        # Child falls through as the daemonized engine.

    cfg = EngineConfig(
        poll_interval=args.poll_interval,
        node_timeout=args.node_timeout,
        workflow_timeout=args.workflow_timeout,
        max_retries=args.max_retries,
        max_node_executions=args.max_node_executions,
    )
    engine = Engine(args.workflow, project_dir, cfg)
    result = engine.run()

    status = result.get("status") if isinstance(result, dict) else None
    pc = result.get("pc") if isinstance(result, dict) else None
    print(f"Workflow finished: status={status}, pc={pc}")
    return 0 if status == "done" else 1


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow resume")
        resume = parser
    else:
        resume = subparsers.add_parser(
            "resume",
            help="Resume a stopped or failed workflow",
        )
    resume.add_argument("workflow", help="Path to workflow YAML file")
    resume.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: directory of the workflow file)",
    )
    resume.add_argument(
        "--from", dest="from_node", default=None,
        help="Resume from a specific node id (overrides state.pc)",
    )
    resume.add_argument(
        "--retry", action="store_true",
        help="Force status back to 'running' even if state isn't a "
             "known failure status (e.g. resume from 'waiting').",
    )
    resume.add_argument(
        "--dry-run", action="store_true",
        help="Apply state edits and print what would happen, but do "
             "not actually run the engine.",
    )
    # Engine knobs — same defaults as the run subcommand.
    resume.add_argument("--poll-interval", type=int, default=5)
    resume.add_argument("--node-timeout", type=int, default=600)
    resume.add_argument("--workflow-timeout", type=int, default=3600)
    resume.add_argument("--max-retries", type=int, default=3)
    resume.add_argument("--max-node-executions", type=int, default=10)
    resume.add_argument(
        "--daemon", action="store_true",
        help="Fork the resumed engine to the background (same as "
             "`camflow run --daemon`). Also spawns a sibling watchdog.",
    )
    resume.add_argument(
        "--no-watchdog", action="store_true",
        help="Skip the sibling watchdog on --daemon resumes.",
    )
    resume.add_argument(
        "--watchdog-max-restarts", type=int, default=None,
        help="Override the watchdog's max-restarts budget.",
    )
    resume.set_defaults(func=resume_command)

    if subparsers is None:
        return parser
    return resume


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
