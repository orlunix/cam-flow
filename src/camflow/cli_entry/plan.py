"""`camflow plan "<request>"` subcommand.

Generates a workflow.yaml from a natural-language request and writes it
to disk. Prints an ASCII graph and any quality warnings.

By default since Phase A, planning runs as an **agent** (a camc-spawned
Claude Code session that explores the project, drafts the yaml,
self-validates, iterates). The legacy single-shot LLM Planner remains
available for one release cycle behind ``--legacy`` so we can compare
behaviour and roll back without a code change if the agent path
misbehaves on a real workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from camflow.planner.agent_planner import (
    PlannerAgentError,
    generate_workflow_via_agent,
)
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


def _load_scout_reports(paths):
    """Load each scout report from disk (or stdin for "-"). Skips broken files
    with a stderr warning rather than failing the whole plan.
    """
    if not paths:
        return None
    reports = []
    for p in paths:
        try:
            if p == "-":
                text = sys.stdin.read()
            else:
                with open(p, encoding="utf-8") as f:
                    text = f.read()
            data = json.loads(text)
        except (OSError, ValueError) as e:
            print(f"[plan] warning: skipping scout report {p}: {e}", file=sys.stderr)
            continue
        # Accept either a single report dict or a list of reports.
        if isinstance(data, list):
            reports.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            reports.append(data)
        else:
            print(f"[plan] warning: scout report {p} is not a dict/list", file=sys.stderr)
    return reports or None


def _resolve_claude_md(arg):
    if arg and os.path.isfile(arg):
        return arg
    for candidate in ("CLAUDE.md", ".claude/CLAUDE.md"):
        if os.path.isfile(candidate):
            return candidate
    return None


def plan_command(args):
    if args.legacy:
        return _plan_legacy(args)
    if getattr(args, "interactive", False):
        return _plan_interactive(args)
    return _plan_via_agent(args)


def _plan_via_agent(args):
    """Default since Phase A: spawn a Planner agent and let it drive
    its own explore / draft / validate loop. The agent writes
    ``.camflow/workflow.yaml`` and ``.camflow/plan-rationale.md``.
    """
    project_dir = os.path.abspath(args.project_dir or os.getcwd())
    print(
        "[plan] spawning Planner agent (this typically takes 30s-2m)...",
        file=sys.stderr,
    )
    try:
        result = generate_workflow_via_agent(
            args.request,
            project_dir=project_dir,
            timeout_seconds=args.timeout,
        )
    except PlannerAgentError as exc:
        print(f"ERROR: Planner agent failed: {exc}", file=sys.stderr)
        print(
            "Fall back to the legacy single-shot Planner with "
            "`camflow plan --legacy`.",
            file=sys.stderr,
        )
        return 1

    print(
        f"[plan] Planner {result.agent_id} finished in "
        f"{result.duration_s:.1f}s",
        file=sys.stderr,
    )
    print(f"[plan] workflow:  {result.workflow_path}", file=sys.stderr)
    if result.rationale_path:
        print(
            f"[plan] rationale: {result.rationale_path}",
            file=sys.stderr,
        )
    if result.warnings:
        print("", file=sys.stderr)
        print("Plan-quality warnings:", file=sys.stderr)
        for w in result.warnings:
            print(f"  - {w}", file=sys.stderr)

    # If the user asked for a custom output path, copy from .camflow/
    # to it. The agent always writes inside .camflow/ for sandboxing.
    if args.output and os.path.abspath(args.output) != result.workflow_path:
        try:
            with open(result.workflow_path, encoding="utf-8") as src:
                content = src.read()
            with open(args.output, "w", encoding="utf-8") as dst:
                dst.write(content)
            print(f"[plan] also copied to {args.output}", file=sys.stderr)
        except OSError as exc:
            print(
                f"WARNING: could not copy to {args.output}: {exc}",
                file=sys.stderr,
            )

    print("", file=sys.stderr)
    print("ASCII graph:", file=sys.stderr)
    print(ascii_graph(result.workflow), file=sys.stderr)
    return 0


def _plan_interactive(args):
    """``camflow plan -i "<request>"`` — spawn the Planner agent and
    let the user converse with it via stdin while waiting for
    workflow.yaml to land.

    Each line you type is forwarded to the agent via ``camc send``.
    The agent's screen is tailed back every 5 s. Ctrl-D switches to
    poll-only (still waits for workflow.yaml). Ctrl-C aborts.
    """
    import select
    import shutil
    import subprocess
    import time
    from pathlib import Path

    import yaml as _yaml

    from camflow import paths as camflow_paths
    from camflow.engine.dsl import validate_workflow as validate_dsl
    from camflow.planner.agent_planner import (
        PROMPT_FILE,
        REQUEST_FILE,
        WORKFLOW_FILE,
        _short_id,
        build_boot_pack,
    )
    from camflow.planner.validator import validate_plan_quality
    from camflow.registry import on_agent_finalized, on_agent_spawned

    CAMC_BIN = shutil.which("camc") or "camc"

    project_dir = os.path.abspath(args.project_dir or os.getcwd())
    request = args.request
    timeout_s = args.timeout

    # Setup: write request + boot pack to the Planner's private dir.
    flow_id = f"planner_{_short_id()}"
    prompt_p = camflow_paths.planner_prompt_path(project_dir, flow_id)
    request_p = camflow_paths.planner_request_path(project_dir, flow_id)
    request_p.write_text(request.rstrip() + "\n", encoding="utf-8")

    rel_prompt = str(prompt_p.relative_to(Path(project_dir)))
    boot_pack = (
        f"# camflow-prompt-path: {rel_prompt}\n\n"
        + build_boot_pack(project_dir, request)
    )
    prompt_p.write_text(boot_pack, encoding="utf-8")

    # Pre-clear stale outputs.
    cf = Path(project_dir) / ".camflow"
    for stale in (WORKFLOW_FILE, "plan-rationale.md"):
        try:
            (cf / stale).unlink(missing_ok=True)
        except OSError:
            pass

    name = f"planner-{_short_id()}"
    started_at = time.time()

    print(
        "[plan -i] spawning Planner agent...", file=sys.stderr,
    )
    try:
        proc = subprocess.run(
            [
                CAMC_BIN, "run",
                "--name", name,
                "--path", project_dir,
                f"Read {rel_prompt} and follow ALL instructions there "
                "exactly. Drop workflow.yaml + plan-rationale.md, "
                "then stop.",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"ERROR: camc run failed: {exc}", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print(
            f"ERROR: camc run exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        return 1

    # Parse agent id from stdout.
    import re as _re
    m = (
        _re.search(r"agent\s+([0-9a-f]{6,12})", proc.stdout)
        or _re.search(r"ID:\s+([0-9a-f]{6,12})", proc.stdout)
    )
    if not m:
        print(
            "ERROR: could not parse agent id from camc output",
            file=sys.stderr,
        )
        return 1
    agent_id = m.group(1)

    # Register in agents.json (best-effort).
    try:
        on_agent_spawned(
            project_dir,
            role="planner",
            agent_id=agent_id,
            spawned_by="camflow plan -i",
            flow_id=None,
            prompt_file=str(prompt_p),
            extra={"name": name},
        )
    except Exception:
        pass

    print(
        f"[plan -i] Planner agent: {agent_id}\n"
        "[plan -i] type a line to send to the agent; Ctrl-D for "
        "poll-only; Ctrl-C aborts.\n",
        file=sys.stderr,
    )

    workflow_path = cf / WORKFLOW_FILE
    deadline = started_at + timeout_s
    last_capture_emit = 0.0
    capture_interval = 5.0
    stdin_open = sys.stdin.isatty()

    def _camc_send(text: str) -> None:
        try:
            subprocess.run(
                [CAMC_BIN, "send", agent_id, text],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _camc_capture_tail(n: int = 8) -> str:
        try:
            p = subprocess.run(
                [CAMC_BIN, "capture", agent_id, "-n", str(n)],
                capture_output=True, text=True, timeout=5,
            )
            if p.returncode == 0:
                return p.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return ""

    def _cleanup() -> None:
        try:
            subprocess.run(
                [CAMC_BIN, "rm", agent_id, "--kill"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        while time.time() < deadline:
            # 1. Has workflow.yaml landed?
            if workflow_path.exists():
                break

            # 2. Periodic screen tail.
            now = time.time()
            if now - last_capture_emit >= capture_interval:
                last_capture_emit = now
                cap = _camc_capture_tail()
                if cap:
                    sys.stderr.write(
                        "\n--- planner screen ----------------\n"
                    )
                    sys.stderr.write(cap)
                    sys.stderr.write(
                        "----------------------------------\n"
                    )

            # 3. Non-blocking stdin read.
            if stdin_open:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 1.0)
                except KeyboardInterrupt:
                    print(
                        "\n[plan -i] Ctrl-C — aborting Planner.",
                        file=sys.stderr,
                    )
                    _cleanup()
                    return 130
                if r:
                    line = sys.stdin.readline()
                    if not line:  # EOF
                        stdin_open = False
                        print(
                            "[plan -i] stdin closed; switching to "
                            "poll-only.",
                            file=sys.stderr,
                        )
                        continue
                    text = line.rstrip("\n")
                    if text:
                        _camc_send(text)
                        sys.stderr.write(f"[plan -i] sent: {text[:80]}\n")
            else:
                time.sleep(1.0)
    except KeyboardInterrupt:
        print(
            "\n[plan -i] Ctrl-C — aborting Planner.", file=sys.stderr,
        )
        _cleanup()
        return 130

    # Workflow.yaml landed (or timeout).
    if not workflow_path.exists():
        print(
            f"ERROR: timed out waiting for {workflow_path} after "
            f"{int(time.time() - started_at)}s",
            file=sys.stderr,
        )
        _cleanup()
        return 1

    # Validate.
    try:
        text = workflow_path.read_text(encoding="utf-8")
        workflow = _yaml.safe_load(text)
    except Exception as exc:
        print(f"ERROR: workflow.yaml parse: {exc}", file=sys.stderr)
        _cleanup()
        return 1
    if not isinstance(workflow, dict) or not workflow:
        print(
            "ERROR: workflow.yaml is empty / not a mapping",
            file=sys.stderr,
        )
        _cleanup()
        return 1
    ok, errors = validate_dsl(workflow)
    if not ok:
        print(
            "ERROR: workflow.yaml fails DSL validation:\n  - "
            + "\n  - ".join(errors),
            file=sys.stderr,
        )
        _cleanup()
        return 1
    quality_errors, quality_warnings = validate_plan_quality(workflow)
    if quality_errors:
        print(
            "ERROR: workflow fails plan-quality validation:\n  - "
            + "\n  - ".join(quality_errors),
            file=sys.stderr,
        )
        _cleanup()
        return 1

    # Mark planner completed in registry.
    try:
        on_agent_finalized(
            project_dir,
            agent_id=agent_id,
            result={"status": "success"},
            duration_ms=int((time.time() - started_at) * 1000),
            result_file=str(workflow_path),
        )
    except Exception:
        pass

    _cleanup()

    duration = time.time() - started_at
    print(
        f"[plan -i] Planner finished in {duration:.1f}s; "
        f"workflow at {workflow_path}",
        file=sys.stderr,
    )
    if quality_warnings:
        print("\nPlan-quality warnings:", file=sys.stderr)
        for w in quality_warnings:
            print(f"  - {w}", file=sys.stderr)
    print("\nASCII graph:", file=sys.stderr)
    print(ascii_graph(workflow), file=sys.stderr)
    return 0


def _plan_legacy(args):
    """Legacy single-shot LLM Planner. Kept available for one release
    cycle so we can roll back from the agent Planner without a code
    change. Behaviour identical to pre-Phase-A ``camflow plan``.
    """
    claude_md = _resolve_claude_md(args.claude_md)
    skills_dir = _resolve_skills_dir(args.skills_dir)

    if claude_md:
        print(f"[plan --legacy] using CLAUDE.md: {claude_md}", file=sys.stderr)
    if skills_dir:
        print(
            f"[plan --legacy] using skills dir: {skills_dir}", file=sys.stderr,
        )

    scout_reports = _load_scout_reports(args.scout_report)
    if scout_reports:
        print(
            f"[plan --legacy] loaded {len(scout_reports)} scout report(s)",
            file=sys.stderr,
        )
    print("[plan --legacy] generating workflow...", file=sys.stderr)

    try:
        workflow = generate_workflow(
            args.request,
            claude_md_path=claude_md,
            skills_dir=skills_dir,
            domain=args.domain,
            agents_dir=args.agents_dir,
            scout_reports=scout_reports,
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

    print(f"[plan --legacy] wrote {out}", file=sys.stderr)
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
    plan.add_argument("--domain", default=None,
                       choices=["hardware", "software", "deployment", "research"],
                       help="Load a domain-specific rule pack into the planner prompt")
    plan.add_argument("--agents-dir", default=None,
                       help="Path to agent definitions directory "
                            "(default: ~/.claude/agents/)")
    plan.add_argument(
        "--scout-report", action="append", default=[],
        help="(legacy mode only) Path to a scout report JSON file. Pass "
             "`-` to read one report from stdin. May be repeated; "
             "capped at MAX_SCOUT_REPORTS (3) entries.",
    )
    plan.add_argument(
        "--legacy", action="store_true",
        help="Use the pre-Phase-A single-shot LLM Planner (one Claude "
             "API call, no agent). Kept for one release cycle as a "
             "fallback while the agent Planner stabilises.",
    )
    plan.add_argument(
        "-i", "--interactive", action="store_true",
        help="Spawn the Planner agent and run an interactive loop: "
             "lines you type are forwarded to the agent via "
             "`camc send`; the agent's screen is tailed back to you "
             "every few seconds until workflow.yaml is written.",
    )
    plan.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd). The agent Planner "
             "writes to <project>/.camflow/.",
    )
    plan.add_argument(
        "--timeout", type=int, default=180,
        help="Seconds the agent Planner has to write workflow.yaml "
             "(default: 180).",
    )
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
