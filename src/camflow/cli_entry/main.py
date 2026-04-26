"""camflow CLI entry point.

Usage modes (the first positional argument decides the mode):

    camflow <workflow.yaml> [flags]         # run a workflow (default)
    camflow run <workflow.yaml> [flags]     # explicit form of the default
    camflow run --daemon <workflow.yaml>    # run in the background (+ watchdog)
    camflow run --daemon --no-watchdog ...  # daemon engine, no watchdog
    camflow resume <workflow.yaml> [flags]  # resume a stopped/failed workflow
    camflow stop [--force]                  # signal a running engine to exit
    camflow status [workflow.yaml]          # report engine liveness + progress
    camflow watchdog <workflow.yaml>        # run the watchdog loop manually
    camflow plan "<request>" [flags]        # generate workflow.yaml from NL
    camflow scout --type ... --query ...    # read-only scout for the planner
    camflow evolve report <dir> [--json]    # trace-based eval reports

Keeping the workflow path as the default first positional argument
preserves backward compatibility with existing scripts like
`camflow examples/cam/workflow.yaml`. The explicit `run` form exists
for parity with the other subcommands (`plan`, `resume`, etc.) and is
what docs/strategy.md recommends in examples.
"""

import argparse
import os
import sys

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.cli_entry.ctl import ctl_command
from camflow.cli_entry.daemon import daemonize_engine, spawn_watchdog
from camflow.cli_entry.evolve import build_parser as build_evolve_parser
from camflow.cli_entry.plan import build_parser as build_plan_parser
from camflow.cli_entry.resume import build_parser as build_resume_parser
from camflow.cli_entry.scout import build_parser as build_scout_parser
from camflow.cli_entry.status import build_parser as build_status_parser
from camflow.cli_entry.status import status_command
from camflow.cli_entry.stop import build_parser as build_stop_parser
from camflow.cli_entry.stop import stop_command
from camflow.engine.dsl import load_workflow, validate_workflow
from camflow.engine.monitor import EngineLockError
from camflow.engine.watchdog import build_parser as build_watchdog_parser
from camflow.engine.watchdog import watchdog_command


RUN_FLAGS = {  # subset so we can help-dispatch cleanly
    "--project-dir", "-p", "--validate", "--dry-run",
    "--force-restart", "--poll-interval", "--node-timeout",
    "--workflow-timeout", "--max-retries", "--max-node-executions",
    "--no-watchdog", "--watchdog-max-restarts",
}


def _build_run_parser():
    parser = argparse.ArgumentParser(
        prog="camflow",
        description="cam-flow: lightweight stateful workflow engine for agent execution",
    )
    parser.add_argument("workflow", help="Path to workflow YAML file")
    parser.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: directory of the workflow file)",
    )
    parser.add_argument("--validate", action="store_true",
                        help="Validate workflow and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Static walk without executing nodes")
    parser.add_argument("--force-restart", action="store_true",
                        help="Discard any current_agent_id on resume")
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--node-timeout", type=int, default=600)
    parser.add_argument("--workflow-timeout", type=int, default=3600)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-node-executions", type=int, default=10)
    parser.add_argument(
        "--daemon", action="store_true",
        help="Fork to background; redirect stdout/stderr to "
             ".camflow/engine.log and write PID to .camflow/engine.pid.",
    )
    parser.add_argument(
        "--no-watchdog", action="store_true",
        help="Skip spawning the sibling watchdog process that auto-restarts "
             "the engine on silent crash. Only meaningful with --daemon.",
    )
    parser.add_argument(
        "--watchdog-max-restarts", type=int, default=None,
        help="Max auto-restarts the watchdog will attempt before giving up "
             "(default: watchdog's own default, currently 3).",
    )
    parser.add_argument(
        "--no-steward", action="store_true",
        help="Skip the project-scoped Steward agent. Engine + watchdog "
             "behave exactly as they did before the Steward existed.",
    )
    return parser


def _run_workflow(argv):
    parser = _build_run_parser()
    args = parser.parse_args(argv)

    workflow = load_workflow(args.workflow)
    valid, errors = validate_workflow(workflow)
    if not valid:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if args.validate:
        print("Workflow is valid.")
        return 0

    project_dir = args.project_dir or os.path.dirname(os.path.abspath(args.workflow)) or "."

    if getattr(args, "daemon", False):
        if args.dry_run:
            print("ERROR: --daemon is incompatible with --dry-run", file=sys.stderr)
            return 1
        parent = daemonize_engine(project_dir)
        if parent:
            # Spawn the sibling watchdog so an engine crash auto-recovers.
            # Must happen in the original parent — the engine daemon has
            # already detached, and the grandchild is the engine itself.
            if not args.no_watchdog:
                spawn_watchdog(
                    args.workflow, project_dir,
                    max_restarts=args.watchdog_max_restarts,
                )
            return 0
        # Fall through as the daemonized child.

    cfg = EngineConfig(
        poll_interval=args.poll_interval,
        node_timeout=args.node_timeout,
        workflow_timeout=args.workflow_timeout,
        max_retries=args.max_retries,
        max_node_executions=args.max_node_executions,
        dry_run=args.dry_run,
        force_restart=args.force_restart,
        no_steward=getattr(args, "no_steward", False),
        # `camflow run` always starts fresh. `--dry-run` is inherently
        # non-mutating so we leave it alone.
        reset=not args.dry_run,
    )
    engine = Engine(args.workflow, project_dir, cfg)
    try:
        result = engine.run()
    except EngineLockError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            "Use `camflow status <workflow.yaml>` to check the live engine, "
            "or wait for it to finish.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        return result if isinstance(result, int) else 0

    status = result.get("status") if isinstance(result, dict) else None
    pc = result.get("pc") if isinstance(result, dict) else None
    print(f"Workflow finished: status={status}, pc={pc}")
    return 0 if status == "done" else 1


def _run_evolve(argv):
    parser = build_evolve_parser(None)
    args = parser.parse_args(argv)
    return args.func(args)


def _run_plan(argv):
    parser = build_plan_parser(None)
    args = parser.parse_args(argv)
    return args.func(args)


def _run_scout(argv):
    parser = build_scout_parser(None)
    args = parser.parse_args(argv)
    return args.func(args)


def _run_resume(argv):
    parser = build_resume_parser(None)
    args = parser.parse_args(argv)
    return args.func(args)


def _run_status(argv):
    parser = build_status_parser(None)
    args = parser.parse_args(argv)
    return status_command(args)


def _run_stop(argv):
    parser = build_stop_parser(None)
    args = parser.parse_args(argv)
    return stop_command(args)


def _run_watchdog(argv):
    parser = build_watchdog_parser(None)
    args = parser.parse_args(argv)
    return watchdog_command(args)


def _run_ctl(argv):
    return ctl_command(argv)


def _print_top_help():
    print(__doc__.strip())
    print(
        "\nSee `camflow <workflow.yaml> --help`, `camflow resume --help`, "
        "`camflow plan --help`, `camflow scout --help`, or "
        "`camflow evolve --help`."
    )


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_top_help()
        sys.exit(0)

    # Dispatch: first positional argument decides the mode
    if argv[0] == "evolve":
        rc = _run_evolve(argv[1:])
    elif argv[0] == "plan":
        rc = _run_plan(argv[1:])
    elif argv[0] == "scout":
        rc = _run_scout(argv[1:])
    elif argv[0] == "resume":
        rc = _run_resume(argv[1:])
    elif argv[0] == "status":
        rc = _run_status(argv[1:])
    elif argv[0] == "stop":
        rc = _run_stop(argv[1:])
    elif argv[0] == "watchdog":
        rc = _run_watchdog(argv[1:])
    elif argv[0] == "ctl":
        rc = _run_ctl(argv[1:])
    elif argv[0] == "run":
        # Explicit `run` is a synonym for the default positional-path
        # mode — same parser, same flags, argv[1] is the workflow.
        rc = _run_workflow(argv[1:])
    else:
        rc = _run_workflow(argv)
    sys.exit(rc)


if __name__ == "__main__":
    main()
