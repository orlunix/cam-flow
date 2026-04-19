"""camflow evolve — trace-based evaluation subcommand.

Subcommands:
    camflow evolve report <traces-dir>    # aggregate traces + print
    camflow evolve report --json <dir>    # same, but emit JSON
"""

import argparse
import json
import sys

from camflow.evolution.rollup import print_report, rollup_all


def evolve_report(args):
    summary = rollup_all(args.traces_dir)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print_report(summary)
    return 0


def build_parser(subparsers=None):
    """Build or attach the evolve parser.

    If `subparsers` is provided, we add an 'evolve' subcommand there.
    Otherwise we return a fresh top-level parser for standalone use.
    """
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow evolve")
        evolve = parser
    else:
        evolve = subparsers.add_parser(
            "evolve",
            help="Trace-based evaluation reports",
        )
    evolve_sub = evolve.add_subparsers(dest="evolve_cmd", required=True)

    report = evolve_sub.add_parser(
        "report",
        help="Aggregate trace.log files under a directory and print a summary",
    )
    report.add_argument("traces_dir", help="Directory (or single trace.log path)")
    report.add_argument("--json", action="store_true",
                        help="Emit JSON instead of ASCII table")
    report.set_defaults(func=evolve_report)

    if subparsers is None:
        return parser
    return evolve


def main(argv=None):
    """Standalone entry point: `python -m camflow.cli_entry.evolve report <dir>`."""
    parser = build_parser(None)
    args = parser.parse_args(argv)
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
