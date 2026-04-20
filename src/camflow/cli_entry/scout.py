"""`camflow scout` subcommand — read-only environment + skill scouting.

Wraps `planner/scouts.py` so the camflow-manager skill (Option B —
no direct API) can spawn scouts via Bash and pipe the JSON report
into `camflow plan --scout-report`.

Examples:

    camflow scout --type skill --query "RTL signal trace analysis"
    camflow scout --type env --query vcs --query smake --query p4
    camflow scout --type env --query path:/home/user/rtl
"""

from __future__ import annotations

import argparse
import json
import sys

from camflow.planner.scouts import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_ENV_CHECKS,
    DEFAULT_SCOUT_TIMEOUT,
    run_env_scout,
    run_skill_scout,
)


def scout_command(args) -> int:
    if args.type == "skill":
        if not args.query:
            print("ERROR: --query is required for skill scouts", file=sys.stderr)
            return 2
        # `--query` is action="append" → list. For skill we use the
        # first entry (joined if more than one was given).
        query = " ".join(args.query)
        report = run_skill_scout(
            query,
            max_candidates=args.max_candidates,
            timeout=args.timeout,
        )
    elif args.type == "env":
        if not args.query:
            print("ERROR: --query is required for env scouts", file=sys.stderr)
            return 2
        report = run_env_scout(
            args.query,
            max_checks=args.max_checks,
            timeout=args.timeout,
        )
    else:  # argparse choices already restrict, kept for safety
        print(f"ERROR: unknown scout type {args.type!r}", file=sys.stderr)
        return 2

    indent = 2 if args.pretty else None
    print(json.dumps(report, indent=indent, sort_keys=True))
    return 0


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow scout")
        scout = parser
    else:
        scout = subparsers.add_parser(
            "scout",
            help="Read-only environment + skill scouting for the planner",
        )
    scout.add_argument(
        "--type", required=True, choices=["skill", "env"],
        help="Which scout to run",
    )
    scout.add_argument(
        "--query", action="append", default=[],
        help="Capability description (skill) or tool/path spec (env). "
             "Repeat for multi-tool env scouts.",
    )
    scout.add_argument(
        "--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
        help="Maximum skill candidates returned (skill scout only).",
    )
    scout.add_argument(
        "--max-checks", type=int, default=DEFAULT_MAX_ENV_CHECKS,
        help="Maximum env probes per call (env scout only).",
    )
    scout.add_argument(
        "--timeout", type=int, default=DEFAULT_SCOUT_TIMEOUT,
        help="Per-subprocess timeout in seconds.",
    )
    scout.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print the JSON output (default: compact, line-safe).",
    )
    scout.set_defaults(func=scout_command)

    if subparsers is None:
        return parser
    return scout


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
