"""``camflow chat`` — talk to the project's Steward.

    camflow chat "现在状况?"        one-shot send + brief reply note
    camflow chat                    same but reads message from stdin
    camflow chat --history          print recent engine→Steward events
                                    from .camflow/steward-events.jsonl
    camflow chat --pending          interactively review pending
                                    confirm-queue entries
                                    (Phase B — see steward.autonomy)

Deferred:

    --inbox             — Steward 'ask-user' queue (Phase B / later)
    --all               — multi-project fan-out (OQ-7, deferred)

Resolution order for "current Steward":

    1. ``--project-dir`` flag → ``<project>/.camflow/steward.json``
    2. cwd has ``.camflow/steward.json`` → use it
    3. otherwise: exit 1 with a hint
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from camflow.steward.spawn import is_steward_alive, load_steward_pointer


CAMC_BIN = shutil.which("camc") or "camc"


# ---- helpers ------------------------------------------------------------


def _resolve_project_dir(explicit: str | None) -> str:
    return os.path.abspath(explicit) if explicit else os.getcwd()


def _resolve_steward(project_dir: str) -> str | None:
    pointer = load_steward_pointer(project_dir)
    if pointer and pointer.get("agent_id"):
        return pointer["agent_id"]
    return None


def _camc_send(agent_id: str, message: str) -> bool:
    """Send a user message via ``camc send <id> <text>``.

    User messages do NOT carry the ``[CAMFLOW EVENT]`` prefix — the
    Steward's prompt distinguishes them by absence of the prefix.
    """
    try:
        proc = subprocess.run(
            [CAMC_BIN, "send", agent_id, message],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- one-shot send ------------------------------------------------------


def _do_send(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    agent_id = _resolve_steward(project_dir)
    if agent_id is None:
        print(
            "camflow chat: no Steward registered for this project. "
            "Run `camflow run <yaml>` once to spawn one, or use "
            "`--project-dir` to point at another project.",
            file=sys.stderr,
        )
        return 1

    if not is_steward_alive(project_dir):
        print(
            f"camflow chat: Steward {agent_id} is dead. "
            "Use `camflow steward restart` to bring it back.",
            file=sys.stderr,
        )
        return 1

    message = args.message
    if message is None:
        # Read from stdin (one block, allows multi-line via heredoc).
        message = sys.stdin.read().rstrip("\n")
    if not message:
        print(
            "camflow chat: empty message; nothing to send.",
            file=sys.stderr,
        )
        return 1

    ok = _camc_send(agent_id, message)
    if not ok:
        print(
            f"camflow chat: camc send to {agent_id} failed.",
            file=sys.stderr,
        )
        return 1

    print(f"sent to {agent_id}.")
    print(
        "Steward replies asynchronously inside its tmux session. "
        f"Use `camc capture {agent_id}` to read its current screen, "
        "or `camflow chat --history` to see recent turns."
    )
    return 0


# ---- history ------------------------------------------------------------


def _read_event_tail(project_dir: str, n: int) -> list[dict[str, Any]]:
    p = Path(project_dir) / ".camflow" / "steward-events.jsonl"
    if not p.exists():
        return []
    lines = [
        ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    out: list[dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _do_history(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    agent_id = _resolve_steward(project_dir)
    if agent_id is None:
        print(
            "camflow chat --history: no Steward for this project.",
            file=sys.stderr,
        )
        return 1

    events = _read_event_tail(project_dir, args.tail)
    if not events:
        print("(no events recorded yet)")
        return 0

    print(f"Last {len(events)} engine→Steward events for {agent_id}:")
    print()
    for ev in events:
        ts = ev.get("ts", "")
        kind = ev.get("type", ev.get("kind", "?"))
        flow = ev.get("flow_id") or "-"
        node = ev.get("node") or "-"
        summary = ev.get("summary") or ev.get("status") or ""
        print(f"  {ts}  {kind:<14}  flow={flow}  node={node}  {summary}")
    return 0


# ---- pending (Phase B confirm-flow review) -----------------------------


def _read_pending(project_dir: str) -> list[dict[str, Any]]:
    p = Path(project_dir) / ".camflow" / "control-pending.jsonl"
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _truncate_pending(project_dir: str) -> None:
    p = Path(project_dir) / ".camflow" / "control-pending.jsonl"
    try:
        p.write_text("", encoding="utf-8")
    except OSError:
        pass


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _is_expired(entry: dict[str, Any], now_iso: str) -> bool:
    """Naive ISO8601 string comparison works because all timestamps
    are UTC ISO8601."""
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    return now_iso > expires_at


def _emit_resolution_trace(
    project_dir: str,
    *,
    verb: str,
    args: dict[str, Any],
    flow_id: str | None,
    resolution: str,
    actor: str = "user",
) -> None:
    from camflow.backend.cam.tracer import build_event_entry
    from camflow.backend.persistence import append_trace_atomic
    import time

    try:
        append_trace_atomic(
            str(Path(project_dir) / ".camflow" / "trace.log"),
            build_event_entry(
                "control_resolution",
                actor=actor,
                flow_id=flow_id,
                ts=time.time(),
                verb=verb,
                args=args,
                resolution=resolution,
            ),
        )
    except Exception:
        pass


def _do_pending(args: argparse.Namespace) -> int:
    """Interactive review of pending confirm-queue entries."""
    project_dir = _resolve_project_dir(args.project_dir)

    pending = _read_pending(project_dir)
    if not pending:
        print("(no pending confirms)")
        return 0

    # Drop expired entries first (timeout-deny per OQ-11 = B). The
    # config's confirm.timeout_minutes is what shaped expires_at when
    # the entry was queued; here we just compare against now.
    from datetime import datetime, timezone
    now_iso = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )

    rejected_path = (
        Path(project_dir) / ".camflow" / "control-rejected.jsonl"
    )
    approved_path = (
        Path(project_dir) / ".camflow" / "control.jsonl"
    )

    surviving: list[dict[str, Any]] = []
    expired = 0
    for entry in pending:
        if _is_expired(entry, now_iso):
            entry_with_outcome = dict(entry)
            entry_with_outcome["resolution"] = "timeout-rejected"
            entry_with_outcome["resolved_at"] = now_iso
            _append_jsonl(rejected_path, entry_with_outcome)
            _emit_resolution_trace(
                project_dir,
                verb=entry.get("verb", "?"),
                args=entry.get("args") or {},
                flow_id=entry.get("flow_id"),
                resolution="timeout-rejected",
                actor="system",
            )
            expired += 1
        else:
            surviving.append(entry)

    if expired:
        print(f"(expired {expired} entry/entries past timeout)")

    if not surviving:
        _truncate_pending(project_dir)
        return 0

    print(f"{len(surviving)} pending confirmation(s):")
    print()

    decisions: list[tuple[dict[str, Any], str]] = []
    for entry in surviving:
        ts = entry.get("ts", "?")
        verb = entry.get("verb", "?")
        verb_args = entry.get("args") or {}
        issued_by = entry.get("issued_by", "?")
        print(f"  ts:        {ts}")
        print(f"  verb:      {verb}")
        print(f"  args:      {verb_args}")
        print(f"  issued by: {issued_by}")
        print(f"  expires:   {entry.get('expires_at', '?')}")

        if args.yes_to_all:
            choice = "y"
        elif args.no_to_all:
            choice = "n"
        else:
            try:
                raw = input("  approve? [y/N/never] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raw = "n"
            choice = raw or "n"
        decisions.append((entry, choice))
        print()

    # Apply decisions: y → control.jsonl + resolution=approved;
    # n → control-rejected.jsonl + resolution=rejected;
    # never → set_override(verb, block) + control-rejected.jsonl.
    from camflow.steward.autonomy import LEVEL_BLOCK, set_override

    approved_count = rejected_count = blocked_count = 0
    for entry, choice in decisions:
        verb = entry.get("verb", "?")
        verb_args = entry.get("args") or {}
        flow_id = entry.get("flow_id")

        if choice in ("y", "yes"):
            approved_entry = {
                "ts": now_iso,
                "verb": verb,
                "args": verb_args,
                "issued_by": entry.get("issued_by", "user"),
                "flow_id": flow_id,
            }
            _append_jsonl(approved_path, approved_entry)
            _emit_resolution_trace(
                project_dir, verb=verb, args=verb_args,
                flow_id=flow_id, resolution="approved",
            )
            approved_count += 1
        elif choice == "never":
            try:
                set_override(project_dir, verb, LEVEL_BLOCK)
            except Exception:
                pass
            rej = dict(entry)
            rej["resolution"] = "blocked-by-user"
            rej["resolved_at"] = now_iso
            _append_jsonl(rejected_path, rej)
            _emit_resolution_trace(
                project_dir, verb=verb, args=verb_args,
                flow_id=flow_id, resolution="blocked-by-user",
            )
            blocked_count += 1
        else:
            rej = dict(entry)
            rej["resolution"] = "rejected"
            rej["resolved_at"] = now_iso
            _append_jsonl(rejected_path, rej)
            _emit_resolution_trace(
                project_dir, verb=verb, args=verb_args,
                flow_id=flow_id, resolution="rejected",
            )
            rejected_count += 1

    _truncate_pending(project_dir)

    print(
        f"approved={approved_count} rejected={rejected_count} "
        f"blocked={blocked_count} (and {expired} expired before review)"
    )
    return 0


# ---- CLI hookup ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camflow chat",
        description="Talk to this project's Steward agent.",
    )
    p.add_argument(
        "message",
        nargs="?",
        default=None,
        help="Message to send. Omit to read from stdin.",
    )
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd).",
    )
    p.add_argument(
        "--history", action="store_true",
        help="Print recent engine→Steward events instead of sending.",
    )
    p.add_argument(
        "--tail", type=int, default=20,
        help="With --history, number of events to show (default: 20).",
    )
    p.add_argument(
        "--pending", action="store_true",
        help="Interactively review pending confirm-queue entries.",
    )
    p.add_argument(
        "--yes-to-all", action="store_true",
        help="With --pending, approve every entry non-interactively.",
    )
    p.add_argument(
        "--no-to-all", action="store_true",
        help="With --pending, reject every entry non-interactively.",
    )
    return p


def chat_command(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pending:
        return int(_do_pending(args))
    if args.history:
        return int(_do_history(args))
    return int(_do_send(args))
