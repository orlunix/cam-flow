"""Read-only ``camflow ctl`` verbs.

Each verb is local-file I/O — no engine roundtrip — so even a stuck
engine can still answer "what do we know?". Steward calls these
autonomously; humans can call them too via ``camflow ctl <verb>``.

Verbs registered here:
  read-state      — pretty-print .camflow/state.json
  read-trace      — print last N trace lines (kind-tagged JSONL)
  read-events     — print last N steward-events.jsonl lines
  read-rationale  — print .camflow/plan-rationale.md
  read-registry   — print .camflow/agents.json (or a summary)

All verbs accept ``--project-dir`` from the parent dispatcher and
behave gracefully when the requested file is missing — they print a
short note to stderr and exit 1, never crash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from camflow.cli_entry.ctl import (
    AUTONOMY_AUTONOMOUS,
    VerbSpec,
    register_verb,
)


# ---- file-tail helper --------------------------------------------------


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last n non-empty lines from a text file. Cheap and
    correct for our trace-log size profile (megabytes, not gigabytes)."""
    if not path.exists():
        return []
    lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if n <= 0 or n >= len(lines):
        return lines
    return lines[-n:]


def _file_missing(label: str, path: Path) -> int:
    sys.stderr.write(f"camflow ctl: no {label} found at {path}\n")
    return 1


# ---- read-state --------------------------------------------------------


def _handle_read_state(args: argparse.Namespace, project_dir: str) -> int:
    path = Path(project_dir) / ".camflow" / "state.json"
    if not path.exists():
        return _file_missing("state.json", path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return 0


def _add_read_state_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json", action="store_true",
        help="Compact JSON instead of pretty-printed.",
    )


# ---- read-trace --------------------------------------------------------


def _handle_read_trace(args: argparse.Namespace, project_dir: str) -> int:
    path = Path(project_dir) / ".camflow" / "trace.log"
    if not path.exists():
        return _file_missing("trace.log", path)
    lines = _tail_lines(path, args.tail)
    if args.kind:
        wanted = set(args.kind)
        kept: list[str] = []
        for ln in lines:
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("kind", "step") in wanted:
                kept.append(ln)
        lines = kept
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return 0


def _add_read_trace_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tail", type=int, default=20,
        help="Number of trailing lines to print (0 or negative = all).",
    )
    p.add_argument(
        "--kind", action="append", default=None,
        help=(
            "Filter to one or more event kinds (repeatable). "
            "e.g. --kind step --kind agent_spawned"
        ),
    )


# ---- read-events -------------------------------------------------------


def _handle_read_events(args: argparse.Namespace, project_dir: str) -> int:
    path = Path(project_dir) / ".camflow" / "steward-events.jsonl"
    if not path.exists():
        return _file_missing("steward-events.jsonl", path)
    lines = _tail_lines(path, args.tail)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return 0


def _add_read_events_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tail", type=int, default=20,
        help="Number of trailing events to print (0 or negative = all).",
    )


# ---- read-rationale ----------------------------------------------------


def _handle_read_rationale(args: argparse.Namespace, project_dir: str) -> int:
    path = Path(project_dir) / ".camflow" / "plan-rationale.md"
    if not path.exists():
        return _file_missing("plan-rationale.md", path)
    sys.stdout.write(path.read_text(encoding="utf-8"))
    if not path.read_text(encoding="utf-8").endswith("\n"):
        sys.stdout.write("\n")
    return 0


# ---- read-registry -----------------------------------------------------


def _handle_read_registry(args: argparse.Namespace, project_dir: str) -> int:
    path = Path(project_dir) / ".camflow" / "agents.json"
    if not path.exists():
        return _file_missing("agents.json", path)

    if args.json:
        sys.stdout.write(path.read_text(encoding="utf-8"))
        if not path.read_text(encoding="utf-8").endswith("\n"):
            sys.stdout.write("\n")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    agents = data.get("agents") or []
    current = data.get("current_steward_id")

    sys.stdout.write(f"Project: {data.get('project_dir', '?')}\n")
    sys.stdout.write(
        f"Current steward: {current or '(none)'}\n"
    )
    sys.stdout.write(f"Agents: {len(agents)}\n")

    if not agents:
        return 0

    # Pretty table
    headers = ("ID", "ROLE", "STATUS", "FLOW", "NODE")
    rows: list[tuple[str, str, str, str, str]] = []
    for a in agents:
        rows.append((
            str(a.get("id", "?")),
            str(a.get("role", "?")),
            str(a.get("status", "?")),
            str(a.get("flow_id") or "-"),
            str(a.get("node_id") or "-"),
        ))
    widths = [
        max(len(headers[i]), max(len(r[i]) for r in rows))
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    sys.stdout.write(fmt.format(*headers) + "\n")
    for r in rows:
        sys.stdout.write(fmt.format(*r) + "\n")
    return 0


def _add_read_registry_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json", action="store_true",
        help="Print raw agents.json instead of the pretty table.",
    )


# ---- registration ------------------------------------------------------


def _register_all() -> None:
    """Register every read-only verb. Imported by ``ctl._load_verb_registrations``."""
    register_verb(VerbSpec(
        name="read-state",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_handle_read_state,
        add_args=_add_read_state_args,
        help="print .camflow/state.json",
    ))
    register_verb(VerbSpec(
        name="read-trace",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_handle_read_trace,
        add_args=_add_read_trace_args,
        help="print last N trace.log entries (filter by --kind)",
    ))
    register_verb(VerbSpec(
        name="read-events",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_handle_read_events,
        add_args=_add_read_events_args,
        help="print last N steward-events.jsonl entries",
    ))
    register_verb(VerbSpec(
        name="read-rationale",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_handle_read_rationale,
        help="print .camflow/plan-rationale.md",
    ))
    register_verb(VerbSpec(
        name="read-registry",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_handle_read_registry,
        add_args=_add_read_registry_args,
        help="print .camflow/agents.json (table or --json)",
    ))


# Register on import so ``ctl._load_verb_registrations`` sees them.
_register_all()
