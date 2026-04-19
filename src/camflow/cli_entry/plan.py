"""`camflow plan "<request>"` subcommand.

Generates a workflow.yaml from a natural-language request and writes it
to disk. Prints an ASCII graph and any quality warnings.
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

from camflow.planner.planner import (
    ascii_graph,
    generate_workflow,
)
from camflow.planner.validator import format_report, validate_plan_quality


def _resolve_skills_dir(arg):
    if arg:
        return arg
    # Auto-detect in common locations
    for candidate in ("skills", os.path.expanduser("~/.claude/skills")):
        if os.path.isdir(candidate):
            return candidate
    return None


def _resolve_claude_md(arg):
    if arg and os.path.isfile(arg):
        return arg
    for candidate in ("CLAUDE.md", ".claude/CLAUDE.md"):
        if os.path.isfile(candidate):
            return candidate
    return None


def plan_command(args):
    claude_md = _resolve_claude_md(args.claude_md)
    skills_dir = _resolve_skills_dir(args.skills_dir)

    if claude_md:
        print(f"[plan] using CLAUDE.md: {claude_md}", file=sys.stderr)
    if skills_dir:
        print(f"[plan] using skills dir: {skills_dir}", file=sys.stderr)
    print(f"[plan] generating workflow...", file=sys.stderr)

    try:
        workflow = generate_workflow(
            args.request,
            claude_md_path=claude_md,
            skills_dir=skills_dir,
        )
    except Exception as exc:
        print(f"ERROR: planner failed: {exc}", file=sys.stderr)
        return 1

    errors, warnings = validate_plan_quality(workflow)
    report = format_report(errors, warnings)
    print(report, file=sys.stderr)

    if errors and not args.force:
        print(
            "ERROR: plan validation failed. Re-run with --force to write "
            "the broken plan anyway.",
            file=sys.stderr,
        )
        return 1

    # Serialize the workflow. yaml.safe_dump with default_flow_style=False
    # gives block format (readable).
    out = args.output or "workflow.yaml"
    serialized = yaml.safe_dump(
        workflow, default_flow_style=False, sort_keys=False, width=120,
    )
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(serialized)
    except OSError as exc:
        print(f"ERROR: could not write {out}: {exc}", file=sys.stderr)
        return 1

    print(f"[plan] wrote {out}", file=sys.stderr)
    print("", file=sys.stderr)
    print("ASCII graph:", file=sys.stderr)
    print(ascii_graph(workflow), file=sys.stderr)

    return 0


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow plan")
        plan = parser
    else:
        plan = subparsers.add_parser(
            "plan",
            help="Generate workflow.yaml from a natural-language request",
        )
    plan.add_argument("request", help="Natural-language task description")
    plan.add_argument("--claude-md", default=None,
                       help="Path to CLAUDE.md (default: auto-detect)")
    plan.add_argument("--skills-dir", default=None,
                       help="Path to skills/ directory (default: auto-detect)")
    plan.add_argument("--output", "-o", default="workflow.yaml",
                       help="Output file path (default: workflow.yaml)")
    plan.add_argument("--force", action="store_true",
                       help="Write the plan even if validation found errors")
    plan.set_defaults(func=plan_command)

    if subparsers is None:
        return parser
    return plan


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
