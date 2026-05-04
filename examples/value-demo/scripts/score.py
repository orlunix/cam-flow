#!/usr/bin/env python3
"""Apply the 100-point A/B rubric to a finished value-demo run.

Usage:
  python score.py <fixture-dir> [--pristine <path>] > score.json

The fixture-dir is the location where the agent (single camc OR
camflow) operated. We post-hoc:
  * run pytest tests/ and pytest tests/invariants/
  * compute a crude diff size vs the pristine fixture
  * detect camflow artifacts (.camflow/run/) for auditability + recovery

Auto-scored rows go into the rubric; manual rows (evidence quality on
baseline transcripts, recovery on baseline) are left null for the
human reviewer to fill.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def run_pytest(fixture: Path, dir_arg: str,
               extra: list[str] | None = None) -> tuple[bool, int, str]:
    cmd = ["pytest", dir_arg, "-q", "--tb=short"]
    if extra:
        cmd.extend(extra)
    proc = subprocess.run(cmd, cwd=fixture, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"(\d+) passed", output)
    count = int(m.group(1)) if m else 0
    return proc.returncode == 0, count, output


def diff_lines_against_pristine(fixture: Path, pristine: Path) -> int:
    """Crude line-level diff size for files OUTSIDE tests/ scripts/ SPEC.md."""
    skip_top = {"tests", "scripts", "__pycache__", ".camflow", ".pytest_cache"}
    changed = 0
    for p in fixture.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(fixture)
        if rel.parts and rel.parts[0] in skip_top:
            continue
        if rel.name == "SPEC.md":
            continue
        pristine_p = pristine / rel
        try:
            new_lines = p.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        if pristine_p.exists():
            try:
                old_lines = pristine_p.read_text().splitlines()
            except UnicodeDecodeError:
                continue
            for ax, bx in zip(old_lines, new_lines):
                if ax != bx:
                    changed += 1
            changed += abs(len(new_lines) - len(old_lines))
        else:
            changed += len(new_lines)
    return changed


def detect_camflow_artifacts(fixture: Path) -> dict:
    run_dir = fixture / ".camflow" / "run"
    if not run_dir.exists():
        return {"present": False}
    trace_path = run_dir / "trace.jsonl"
    events: list[dict] = []
    if trace_path.exists():
        for line in trace_path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    retries = sum(1 for e in events if e.get("event") == "retry_triggered")
    halts = sum(1 for e in events if e.get("event") == "workflow_halted")
    attempts = 0
    nodes_dir = run_dir / "nodes"
    node_ids: list[str] = []
    if nodes_dir.exists():
        for sub in nodes_dir.iterdir():
            if sub.is_dir():
                node_ids.append(sub.name)
                attempts += sum(1 for c in sub.iterdir()
                                if c.name.startswith("attempt-"))

    # Resilience signals (per the user-corrected semantics: retry is not
    # a positive target; first-pass correctness is. We track WHETHER
    # bounded retry was configured and HOW the workflow ended, not
    # whether retry fired.)
    workflow_yaml_path = run_dir / "workflow.yaml"
    nodes_with_bounded_retry: list[str] = []
    skill_nodes: list[str] = []
    total_retry_budget = 0
    if workflow_yaml_path.exists():
        try:
            import yaml as _yaml
            wf = _yaml.safe_load(workflow_yaml_path.read_text())
            for n in (wf.get("nodes") or []):
                if not isinstance(n, dict):
                    continue
                run_cfg = n.get("run") or {}
                if "skill" in run_cfg:
                    skill_nodes.append(n.get("id", "?"))
                rmax = int(n.get("retry", 1))
                total_retry_budget += max(0, rmax - 1)  # retry:1 = no retries
                if rmax >= 2:
                    nodes_with_bounded_retry.append(n.get("id", "?"))
        except (ImportError, Exception):
            pass

    halt_path = run_dir / "halt.json"
    halt_info: dict = {}
    if halt_path.exists():
        try:
            halt_info = json.loads(halt_path.read_text())
        except json.JSONDecodeError:
            pass
    halt_has_feedback = False
    if halt_info:
        env = halt_info.get("envelope") or {}
        fb = env.get("feedback") or ""
        err_msg = (env.get("error") or {}).get("message") or ""
        halt_has_feedback = bool(fb.strip() or err_msg.strip())

    return {
        "present": True,
        "trace_events": len(events),
        "retry_triggered": retries,
        "workflow_halted": halts,
        "attempts_total": attempts,
        "node_ids": sorted(node_ids),
        "skill_nodes": sorted(skill_nodes),
        "nodes_with_bounded_retry": sorted(nodes_with_bounded_retry),
        "total_retry_budget": total_retry_budget,
        "halted": halt_path.exists(),
        "halt_has_actionable_feedback": halt_has_feedback,
    }


def score_run(fixture: Path, pristine: Path) -> dict:
    visible_pass, visible_count, _ = run_pytest(
        fixture, "tests/", extra=["--ignore=tests/invariants"]
    )
    invariant_pass, invariant_count, _ = run_pytest(
        fixture, "tests/invariants/"
    )
    diff_lines = diff_lines_against_pristine(fixture, pristine)
    cf = detect_camflow_artifacts(fixture)

    # Requirement coverage (35): 1 visible req + 3 invariant reqs.
    # Per-test, NOT all-or-none: if 2 of 3 invariants pass, those 2
    # requirements are satisfied. Pytest exits non-zero whenever any
    # test in the run failed, so we must NOT gate on the suite's
    # exit status here — only on the parsed "N passed" count.
    visible_passed = min(visible_count, 1)
    invariant_passed = min(invariant_count, 3)
    req_pts = round(35 * (visible_passed + invariant_passed) / 4)

    # Test correctness (20).
    if visible_pass and invariant_pass:
        test_pts = 20
    elif visible_pass:
        test_pts = 14
    else:
        test_pts = 0

    # Process auditability (15).
    if cf["present"]:
        # 5 nodes × 1+ attempts each ≈ 5 minimum; trace events many.
        if cf["trace_events"] >= 10 and cf["attempts_total"] >= 5:
            audit_pts = 15
        elif cf["trace_events"] >= 5:
            audit_pts = 10
        else:
            audit_pts = 6
    else:
        audit_pts = 1  # transcript-only

    # Robustness / minimality (10): smaller diff = better.
    if diff_lines == 0:
        robust_pts = 0
    elif diff_lines <= 30:
        robust_pts = 10
    elif diff_lines <= 80:
        robust_pts = 7
    elif diff_lines <= 200:
        robust_pts = 4
    else:
        robust_pts = 1

    # Resilience / recovery readiness (5).
    #
    # Per the user-corrected semantics (codex-retry-semantics-correction):
    # retry is a bounded safety net, NOT a positive target. First-pass
    # correctness with bounded retry CONFIGURED is the ideal — retry
    # firing is a signal that something failed, not a feature.
    #
    # Auto-scoring rule (camflow side):
    #   5 — lifecycle done + at least one skill node has retry: ≥2
    #       (bounded recovery wired in case of failure; first-pass
    #       success means it wasn't needed)
    #   4 — lifecycle halted with halt.json carrying actionable feedback
    #       (recovery readiness was useful: clean halt with retryable
    #       diagnostic, operator can `camflow resume --feedback ...`)
    #   3 — lifecycle done but no node has bounded retry (recovery not
    #       pre-wired; future failure would have nowhere to go)
    #   1 — lifecycle halted without actionable feedback
    # Penalty: subtract 1 if retry_triggered count is excessive
    # (> 2 × bounded-retry node count) — that's churn, not recovery.
    # Baseline (no .camflow/run/) → manual (transcript inspection).
    if cf["present"]:
        has_bounded_retry = bool(cf.get("nodes_with_bounded_retry"))
        if not cf.get("halted"):
            resilience_pts: int | None = 5 if has_bounded_retry else 3
        elif cf.get("halt_has_actionable_feedback"):
            resilience_pts = 4
        else:
            resilience_pts = 1
        # Churn penalty.
        n_retry_nodes = len(cf.get("nodes_with_bounded_retry") or [])
        if (resilience_pts is not None
                and cf["retry_triggered"] > 2 * max(1, n_retry_nodes)):
            resilience_pts = max(0, resilience_pts - 1)
    else:
        resilience_pts = None  # baseline: human reads transcript

    return {
        "fixture_dir": str(fixture),
        "tests_visible": {"pass": visible_pass, "count": visible_count},
        "tests_invariants": {"pass": invariant_pass,
                             "count": invariant_count},
        "diff_lines": diff_lines,
        "camflow": cf,
        "rubric": {
            "requirement_coverage": {
                "weight": 35, "auto_pts": req_pts,
            },
            "test_correctness": {
                "weight": 20, "auto_pts": test_pts,
            },
            "evidence_quality": {
                "weight": 15, "manual_pts": None,
                "note": ("count concrete file:line/quote citations: "
                         "in transcript (baseline) or "
                         ".camflow/run/nodes/*/attempt-*/agent_output.json "
                         "(camflow). cap at 15."),
            },
            "process_auditability": {
                "weight": 15, "auto_pts": audit_pts,
            },
            "robustness_minimality": {
                "weight": 10, "auto_pts": robust_pts,
            },
            "resilience": {
                "weight": 5,
                "auto_pts": resilience_pts,  # null for baseline
                "note": ("camflow: 5 = first-pass done + bounded retry "
                         "configured; 4 = clean halt w/ actionable "
                         "feedback; 3 = done but no bounded retry; "
                         "1 = halt without feedback. Churn penalty "
                         "subtracts 1 if retry_triggered > 2 × "
                         "bounded-retry-node count. Baseline: human "
                         "reads transcript for self-correction or "
                         "clean give-up."),
            },
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("fixture_dir")
    p.add_argument("--pristine", default=None,
                   help="path to pristine fixture/ (default: this "
                        "repo's examples/value-demo/fixture)")
    args = p.parse_args()
    fixture = Path(args.fixture_dir).resolve()
    if not fixture.is_dir():
        print(f"ERROR: not a directory: {fixture}", file=sys.stderr)
        return 1
    pristine = (Path(args.pristine).resolve() if args.pristine
                else Path(__file__).resolve().parent.parent / "fixture")
    print(json.dumps(score_run(fixture, pristine), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
