"""camflow CLI entry point.

Usage modes (the first positional argument decides the mode):

    camflow <workflow.yaml> [flags]        # run a workflow (default)
    camflow plan "<request>" [flags]       # generate workflow.yaml from NL
    camflow scout --type ... --query ...   # read-only scout for the planner
    camflow evolve report <dir> [--json]   # trace-based eval reports

Keeping the workflow path as the default first positional argument
preserves backward compatibility with existing scripts like
`camflow examples/cam/workflow.yaml`.
"""

import argparse
import os
import sys

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.cli_entry.evolve import build_parser as build_evolve_parser
from camflow.cli_entry.plan import build_parser as build_plan_parser
from camflow.cli_entry.scout import build_parser as build_scout_parser
from camflow.engine.dsl import load_workflow, validate_workflow


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
    result = engine.run()

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


def _print_top_help():
    print(__doc__.strip())
    print(
        "\nSee `camflow <workflow.yaml> --help`, `camflow plan --help`, "
        "`camflow scout --help`, or `camflow evolve --help`."
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
    else:
        rc = _run_workflow(argv)
    sys.exit(rc)


if __name__ == "__main__":
    main()
