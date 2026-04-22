"""camflow CLI entry point.

Usage modes (the first positional argument decides the mode):

    camflow <workflow.yaml> [flags]         # run a workflow (default)
    camflow run <workflow.yaml> [flags]     # explicit form of the default
    camflow run --daemon <workflow.yaml>    # run in the background
    camflow resume <workflow.yaml> [flags]  # resume a stopped/failed workflow
    camflow status <workflow.yaml>          # report engine liveness + progress
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
from camflow.cli_entry.evolve import build_parser as build_evolve_parser
from camflow.cli_entry.plan import build_parser as build_plan_parser
from camflow.cli_entry.resume import build_parser as build_resume_parser
from camflow.cli_entry.scout import build_parser as build_scout_parser
from camflow.cli_entry.status import build_parser as build_status_parser
from camflow.cli_entry.status import status_command
from camflow.engine.dsl import load_workflow, validate_workflow
from camflow.engine.monitor import EngineLockError


RUN_FLAGS = {  # subset so we can help-dispatch cleanly
    "--project-dir", "-p", "--validate", "--dry-run",
    "--force-restart", "--poll-interval", "--node-timeout",
    "--workflow-timeout", "--max-retries", "--max-node-executions",
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
    return parser


def _daemonize(project_dir):
    """Detach the current process to run in the background.

    Minimal POSIX double-fork. Parent exits (0) after printing the
    child's pid; child becomes session leader, closes stdio, and
    redirects stdout/stderr to ``.camflow/engine.log``. Parent returns
    True so the caller can exit; child returns False and keeps running.
    """
    camflow_dir = os.path.join(project_dir, ".camflow")
    os.makedirs(camflow_dir, exist_ok=True)
    log_path = os.path.join(camflow_dir, "engine.log")
    pid_path = os.path.join(camflow_dir, "engine.pid")

    pid = os.fork()
    if pid > 0:
        # Original parent — print child pid, let it go.
        print(f"camflow daemon started (pid {pid}); logs at {log_path}")
        return True

    # First child: detach from controlling terminal.
    os.setsid()
    pid = os.fork()
    if pid > 0:
        # Intermediate parent exits; prevents re-acquiring a TTY.
        os._exit(0)

    # Grandchild: the actual daemon.
    os.chdir(project_dir)
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    # Redirect stdio. Keep stdout/stderr pointed at engine.log so a
    # crash message still lands somewhere readable.
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    log_fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    return False


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
        parent = _daemonize(project_dir)
        if parent:
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
    elif argv[0] == "run":
        # Explicit `run` is a synonym for the default positional-path
        # mode — same parser, same flags, argv[1] is the workflow.
        rc = _run_workflow(argv[1:])
    else:
        rc = _run_workflow(argv)
    sys.exit(rc)


if __name__ == "__main__":
    main()
