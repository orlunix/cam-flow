"""Trace rollup — per-node and per-methodology statistics.

Reads one or more `trace.log` files (JSONL produced by the CAM
engine) and aggregates them into a report. This is the measurement
half of `docs/evaluation.md`: we compute metrics from trace fields
alone — no instrumentation beyond what `build_trace_entry` already
writes.

Public API:
    rollup_trace(trace_path) -> dict       # single trace
    rollup_all(traces_dir)   -> dict       # merge across many traces
    print_report(summary)    -> None       # human-readable ASCII
"""

from __future__ import annotations

import glob
import json
import os
import statistics
from collections import defaultdict


# ---- single trace --------------------------------------------------------


def _load_trace(path):
    """Yield one parsed entry per JSONL line. Skip malformed trailing lines."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def _new_bucket():
    return {
        "runs": 0,
        "successes": 0,
        "fails": 0,
        "durations_ms": [],
        "prompt_tokens": [],
        "methodologies": defaultdict(int),
        "exec_modes": defaultdict(int),
        "retry_modes": defaultdict(int),
        "escalation_levels": defaultdict(int),
    }


def _record_entry(bucket, entry):
    bucket["runs"] += 1
    status = (entry.get("node_result") or {}).get("status", "unknown")
    if status == "success":
        bucket["successes"] += 1
    elif status == "fail":
        bucket["fails"] += 1

    dur = entry.get("duration_ms")
    if isinstance(dur, (int, float)):
        bucket["durations_ms"].append(dur)

    pt = entry.get("prompt_tokens")
    if isinstance(pt, (int, float)) and pt > 0:
        bucket["prompt_tokens"].append(pt)

    methodology = entry.get("methodology") or "none"
    bucket["methodologies"][methodology] += 1

    exec_mode = entry.get("exec_mode") or "unknown"
    bucket["exec_modes"][exec_mode] += 1

    retry_mode = entry.get("retry_mode")
    if retry_mode:
        bucket["retry_modes"][retry_mode] += 1

    level = entry.get("escalation_level")
    if isinstance(level, int):
        bucket["escalation_levels"][level] += 1


def _finalize_bucket(bucket):
    """Convert intermediate aggregation into a flat summary dict."""
    runs = bucket["runs"]
    successes = bucket["successes"]
    durations = bucket["durations_ms"]
    tokens = bucket["prompt_tokens"]

    return {
        "runs": runs,
        "successes": successes,
        "fails": bucket["fails"],
        "success_rate": (successes / runs) if runs else 0.0,
        "avg_duration_ms": statistics.mean(durations) if durations else None,
        "median_duration_ms": statistics.median(durations) if durations else None,
        "avg_prompt_tokens": statistics.mean(tokens) if tokens else None,
        "methodologies": dict(bucket["methodologies"]),
        "exec_modes": dict(bucket["exec_modes"]),
        "retry_modes": dict(bucket["retry_modes"]),
        "escalation_levels": dict(bucket["escalation_levels"]),
    }


def rollup_trace(trace_path):
    """Read one trace.log and compute per-node statistics.

    Returns:
        {
          "source": <path>,
          "steps": <total step count>,
          "final_status": <status of last entry, or None>,
          "nodes": {<node_id>: {<stats>}, ...},
          "methodologies": {<label>: {<stats>}, ...},
          "overall": {<stats across all entries>},
        }
    """
    node_buckets = defaultdict(_new_bucket)
    methodology_buckets = defaultdict(_new_bucket)
    overall_bucket = _new_bucket()

    total_steps = 0
    last_entry = None
    for entry in _load_trace(trace_path):
        # Filter to per-step entries. Trace v2 also contains agent
        # lifecycle, file ops, and control events (kind != "step").
        # Old entries pre-dating the kind field have no key; those
        # are steps too.
        if entry.get("kind", "step") != "step":
            continue
        total_steps += 1
        last_entry = entry
        node_id = entry.get("node_id", "unknown")
        methodology = entry.get("methodology") or "none"

        _record_entry(node_buckets[node_id], entry)
        _record_entry(methodology_buckets[methodology], entry)
        _record_entry(overall_bucket, entry)

    final_status = None
    if last_entry is not None:
        transition = last_entry.get("transition") or {}
        final_status = transition.get("workflow_status") or (
            (last_entry.get("node_result") or {}).get("status")
        )

    return {
        "source": trace_path,
        "steps": total_steps,
        "final_status": final_status,
        "nodes": {k: _finalize_bucket(v) for k, v in node_buckets.items()},
        "methodologies": {k: _finalize_bucket(v) for k, v in methodology_buckets.items()},
        "overall": _finalize_bucket(overall_bucket),
    }


# ---- aggregate across many traces ----------------------------------------


def _find_trace_files(traces_dir):
    """Find trace.log files under `traces_dir`, depth-first.

    Matches: traces_dir itself (if it's a file), traces_dir/**/trace.log,
    traces_dir/*.log.
    """
    if os.path.isfile(traces_dir):
        return [traces_dir]
    patterns = [
        os.path.join(traces_dir, "trace.log"),
        os.path.join(traces_dir, ".camflow", "trace.log"),
        os.path.join(traces_dir, "**", "trace.log"),
        os.path.join(traces_dir, "**", ".camflow", "trace.log"),
    ]
    found = []
    for pat in patterns:
        for path in glob.glob(pat, recursive=True):
            if path not in found and os.path.isfile(path):
                found.append(path)
    return found


def _merge_bucket(dst, src):
    """Merge a finalized bucket into an intermediate bucket for re-summing."""
    dst["runs"] += src["runs"]
    dst["successes"] += src["successes"]
    dst["fails"] += src["fails"]
    # For durations and tokens, we keep their per-trace averages and weight
    # simply by runs when re-averaging. Keep them in list form.
    if src.get("avg_duration_ms") is not None:
        dst["durations_ms"].extend([src["avg_duration_ms"]] * src["runs"])
    if src.get("avg_prompt_tokens") is not None:
        dst["prompt_tokens"].extend([src["avg_prompt_tokens"]] * src["runs"])
    for k, v in src.get("methodologies", {}).items():
        dst["methodologies"][k] += v
    for k, v in src.get("exec_modes", {}).items():
        dst["exec_modes"][k] += v
    for k, v in src.get("retry_modes", {}).items():
        dst["retry_modes"][k] += v
    for k, v in src.get("escalation_levels", {}).items():
        dst["escalation_levels"][int(k)] += v


def rollup_all(traces_dir):
    """Aggregate statistics across every trace.log under `traces_dir`.

    Returns a dict with the same shape as `rollup_trace` plus a
    `trace_count` field indicating how many logs were merged.
    """
    files = _find_trace_files(traces_dir)
    if not files:
        return {
            "source": traces_dir,
            "trace_count": 0,
            "steps": 0,
            "final_status": None,
            "nodes": {},
            "methodologies": {},
            "overall": _finalize_bucket(_new_bucket()),
            "per_trace": [],
        }

    per_trace = []
    agg_nodes = defaultdict(_new_bucket)
    agg_methods = defaultdict(_new_bucket)
    agg_overall = _new_bucket()

    for path in files:
        single = rollup_trace(path)
        per_trace.append({
            "source": single["source"],
            "steps": single["steps"],
            "final_status": single["final_status"],
            "overall": single["overall"],
        })
        for node_id, finalized in single["nodes"].items():
            _merge_bucket(agg_nodes[node_id], finalized)
        for label, finalized in single["methodologies"].items():
            _merge_bucket(agg_methods[label], finalized)
        _merge_bucket(agg_overall, single["overall"])

    return {
        "source": traces_dir,
        "trace_count": len(files),
        "steps": agg_overall["runs"],
        "final_status": None,  # meaningless across multiple runs
        "nodes": {k: _finalize_bucket(v) for k, v in agg_nodes.items()},
        "methodologies": {k: _finalize_bucket(v) for k, v in agg_methods.items()},
        "overall": _finalize_bucket(agg_overall),
        "per_trace": per_trace,
    }


# ---- report rendering ---------------------------------------------------


def _fmt_rate(stats):
    runs = stats["runs"] or 1
    pct = 100.0 * stats["successes"] / runs
    return f"{pct:5.1f}%"


def _fmt_duration(stats):
    dur = stats.get("avg_duration_ms")
    if dur is None:
        return "     —"
    if dur >= 1000:
        return f"{dur/1000:6.1f}s"
    return f"{dur:6.0f}ms"


def _fmt_tokens(stats):
    t = stats.get("avg_prompt_tokens")
    if t is None:
        return "    —"
    if t >= 1000:
        return f"{t/1000:4.1f}k"
    return f"{t:5.0f}"


def _top_methodology(stats):
    methods = stats.get("methodologies") or {}
    # Exclude "none" unless it's the only entry
    interesting = {k: v for k, v in methods.items() if k != "none"}
    if interesting:
        return max(interesting, key=interesting.get)
    return "-"


def print_report(summary, out=print):
    """Pretty-print a rollup summary to stdout (or via `out` callable)."""
    src = summary.get("source", "<unknown>")
    tc = summary.get("trace_count")
    overall = summary.get("overall", {})

    header = f"cam-flow trace rollup — {src}"
    out(header)
    out("=" * len(header))

    if tc is not None:
        out(f"Traces aggregated:  {tc}")
    out(f"Total steps:        {summary.get('steps', 0)}")
    if summary.get("final_status"):
        out(f"Final status:       {summary['final_status']}")

    runs = overall.get("runs", 0)
    if runs:
        out(f"Overall success:    {_fmt_rate(overall)}")
        if overall.get("avg_duration_ms") is not None:
            out(f"Avg duration/step:  {_fmt_duration(overall).strip()}")

    # Per-node table
    nodes = summary.get("nodes") or {}
    if nodes:
        out("")
        out(f"{'Node':<16} {'Runs':>5} {'Success':>8} {'AvgDur':>8} {'AvgTok':>7} {'Methodology':>15} {'ExecMode':>10}")
        out("-" * 78)
        for node_id in sorted(nodes):
            stats = nodes[node_id]
            exec_modes = stats.get("exec_modes") or {}
            top_exec = max(exec_modes, key=exec_modes.get) if exec_modes else "-"
            out(
                f"{node_id:<16} {stats['runs']:>5} {_fmt_rate(stats):>8} "
                f"{_fmt_duration(stats):>8} {_fmt_tokens(stats):>7} "
                f"{_top_methodology(stats):>15} {top_exec:>10}"
            )

    # Per-methodology table
    methods = summary.get("methodologies") or {}
    if len(methods) > 1 or (methods and "none" not in methods):
        out("")
        out(f"{'Methodology':<20} {'Runs':>5} {'Success':>8} {'AvgDur':>8}")
        out("-" * 42)
        for label in sorted(methods):
            stats = methods[label]
            out(
                f"{label:<20} {stats['runs']:>5} {_fmt_rate(stats):>8} "
                f"{_fmt_duration(stats):>8}"
            )

    # Retry-mode and escalation summary
    retry_modes = overall.get("retry_modes") or {}
    escalation = overall.get("escalation_levels") or {}
    if retry_modes or escalation:
        out("")
        if retry_modes:
            out("Retry modes: " + ", ".join(f"{k}={v}" for k, v in sorted(retry_modes.items())))
        if escalation:
            out("Escalation levels: " + ", ".join(f"L{k}={v}" for k, v in sorted(escalation.items())))
