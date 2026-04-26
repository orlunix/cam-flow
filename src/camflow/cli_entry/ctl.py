"""``camflow ctl`` — Steward's verb dispatcher.

The narrow interface through which the Steward (and any human) influences
a running engine. Verbs come in two flavors:

  - **autonomous** — run inline, return their result immediately. Read
    verbs (``read-state``, ``read-trace``, ...) live here.
  - **confirm** — queued to ``.camflow/control-pending.jsonl`` and only
    drained by the engine after a human approves them via
    ``camflow chat --pending``. Risky verbs (``replan``, ``spawn``,
    ``skip``) live here. See ``docs/design-next-phase.md`` §7.6.

Verbs register themselves at module import via ``register_verb`` from the
verb implementation files (``ctl_read.py``, etc.). The dispatcher stays
generic — adding a verb does not require editing this file.

Public API:
    register_verb(spec)
    dispatch(verb_name, argv, project_dir) -> int   # exit code
    build_parser() / ctl_command(args)               # CLI hookup
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from camflow.backend.cam.tracer import build_event_entry
from camflow.backend.persistence import append_trace_atomic


CONTROL_QUEUE = "control.jsonl"
CONTROL_PENDING = "control-pending.jsonl"
CONTROL_REJECTED = "control-rejected.jsonl"

# Autonomy classifications. ``autonomous`` verbs run inline; ``confirm``
# verbs go to the pending queue and need human ack before the engine
# drains them. Anything else is a programming error.
AUTONOMY_AUTONOMOUS = "autonomous"
AUTONOMY_CONFIRM = "confirm"
_VALID_AUTONOMY = (AUTONOMY_AUTONOMOUS, AUTONOMY_CONFIRM)


@dataclass(frozen=True)
class VerbSpec:
    """Schema for one ``ctl`` verb.

    Fields:
      name        — string used on the CLI: ``camflow ctl <name>``.
      autonomy    — ``"autonomous"`` (run inline) or ``"confirm"`` (queue).
      handler     — for autonomous verbs: ``(args, project_dir) -> int``;
                    for confirm verbs: ignored (we just queue the args).
      add_args    — function taking an ``argparse.ArgumentParser`` and
                    adding the verb's flags. Optional.
      help        — one-line description shown in the verb listing.
    """

    name: str
    autonomy: str
    handler: Callable[[argparse.Namespace, str], int] | None = None
    add_args: Callable[[argparse.ArgumentParser], None] | None = None
    help: str = ""

    def __post_init__(self) -> None:
        if self.autonomy not in _VALID_AUTONOMY:
            raise ValueError(
                f"verb {self.name!r}: autonomy must be one of "
                f"{_VALID_AUTONOMY}, got {self.autonomy!r}"
            )
        if self.autonomy == AUTONOMY_AUTONOMOUS and self.handler is None:
            raise ValueError(
                f"verb {self.name!r}: autonomous verbs require a handler"
            )


# Verb registry. Implementations call ``register_verb`` at import time.
VERBS: dict[str, VerbSpec] = {}


def register_verb(spec: VerbSpec) -> None:
    """Register a verb. Raises if the name is already taken."""
    if spec.name in VERBS:
        raise ValueError(f"verb {spec.name!r} already registered")
    VERBS[spec.name] = spec


def list_verb_names() -> list[str]:
    return sorted(VERBS.keys())


# ---- queue paths -------------------------------------------------------


def _control_path(project_dir: str | os.PathLike, name: str) -> str:
    return str(Path(project_dir) / ".camflow" / name)


def queue_pending(
    project_dir: str | os.PathLike,
    *,
    verb: str,
    args: dict[str, Any],
    issued_by: str = "user",
    flow_id: str | None = None,
    timeout_minutes: int = 30,
) -> dict[str, Any]:
    """Append a confirm-required command to ``control-pending.jsonl``.

    Returns the queued entry dict. Also emits a ``control_command``
    trace entry with ``queue="pending"``.
    """
    now = time.time()
    expires_at = now + timeout_minutes * 60
    entry = {
        "ts": _iso(now),
        "expires_at": _iso(expires_at),
        "verb": verb,
        "args": args,
        "issued_by": issued_by,
        "flow_id": flow_id,
    }
    Path(_control_path(project_dir, ".camflow")).parent.mkdir(
        parents=True, exist_ok=True
    )
    pending_path = _control_path(project_dir, CONTROL_PENDING)
    Path(pending_path).parent.mkdir(parents=True, exist_ok=True)
    with open(pending_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    _emit_control_trace(
        project_dir,
        verb=verb,
        args=args,
        actor=issued_by,
        flow_id=flow_id,
        queue="pending",
    )
    return entry


def queue_approved(
    project_dir: str | os.PathLike,
    *,
    verb: str,
    args: dict[str, Any],
    issued_by: str = "user",
    flow_id: str | None = None,
) -> dict[str, Any]:
    """Append an autonomous (or already-approved) command to
    ``control.jsonl`` for the engine to drain on its next tick.

    Emits a ``control_command`` trace entry with ``queue="approved"``.
    """
    entry = {
        "ts": _iso(time.time()),
        "verb": verb,
        "args": args,
        "issued_by": issued_by,
        "flow_id": flow_id,
    }
    queue_path = _control_path(project_dir, CONTROL_QUEUE)
    Path(queue_path).parent.mkdir(parents=True, exist_ok=True)
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    _emit_control_trace(
        project_dir,
        verb=verb,
        args=args,
        actor=issued_by,
        flow_id=flow_id,
        queue="approved",
    )
    return entry


def _emit_control_trace(
    project_dir: str | os.PathLike,
    *,
    verb: str,
    args: dict[str, Any],
    actor: str,
    flow_id: str | None,
    queue: str,
) -> None:
    trace_path = str(Path(project_dir) / ".camflow" / "trace.log")
    Path(trace_path).parent.mkdir(parents=True, exist_ok=True)
    append_trace_atomic(
        trace_path,
        build_event_entry(
            "control_command",
            actor=actor,
            flow_id=flow_id,
            ts=time.time(),
            verb=verb,
            args=args,
            queue=queue,
        ),
    )


def _iso(ts_float: float) -> str:
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---- dispatcher --------------------------------------------------------


def _resolve_project_dir(explicit: str | None) -> str:
    if explicit is not None:
        return os.path.abspath(explicit)
    return os.getcwd()


def dispatch(verb_name: str, argv: list[str], project_dir: str | None = None) -> int:
    """Run one ``ctl`` invocation. Returns an exit code.

    Autonomous verbs run their handler. Confirm verbs queue to the
    pending file and return 0 (the engine + user reconcile later).
    """
    pdir = _resolve_project_dir(project_dir)

    if verb_name not in VERBS:
        sys.stderr.write(
            f"camflow ctl: unknown verb {verb_name!r}.\n"
            f"Known verbs: {', '.join(list_verb_names()) or '(none registered)'}\n"
        )
        return 2

    spec = VERBS[verb_name]
    parser = argparse.ArgumentParser(prog=f"camflow ctl {verb_name}")
    if spec.add_args is not None:
        spec.add_args(parser)

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse already wrote the usage error to stderr.
        return int(e.code) if isinstance(e.code, int) else 2

    if spec.autonomy == AUTONOMY_AUTONOMOUS:
        try:
            return int(spec.handler(args, pdir))
        except Exception as exc:  # noqa: BLE001 — surface CLI error
            sys.stderr.write(
                f"camflow ctl {verb_name}: error: {exc}\n"
            )
            return 1

    # autonomy == "confirm" → queue and exit 0
    queue_pending(
        pdir,
        verb=verb_name,
        args=vars(args),
        issued_by=os.environ.get("CAMFLOW_CTL_ACTOR", "user"),
        flow_id=os.environ.get("CAMFLOW_CTL_FLOW_ID"),
    )
    sys.stdout.write(
        f"queued {verb_name} for confirmation; "
        f"run `camflow chat --pending` to approve.\n"
    )
    return 0


# ---- CLI hookup --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser for ``camflow ctl ...``.

    The verb itself is the first positional; subsequent args are passed
    through to the per-verb sub-parser. We do this manually because
    verbs register lazily and may add custom flags.
    """
    parser = argparse.ArgumentParser(
        prog="camflow ctl",
        description="Run a control verb (Steward / human → engine).",
    )
    parser.add_argument(
        "verb",
        nargs="?",
        help="Name of the verb (omit to list all verbs).",
    )
    parser.add_argument(
        "--project-dir",
        "-p",
        default=None,
        help="Project directory (default: cwd)",
    )
    return parser


def ctl_command(argv: list[str]) -> int:
    """Entry point used by ``camflow.cli_entry.main``."""
    # Lazy-load verb registrations so importing this module is cheap.
    _load_verb_registrations()

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    verb_name, rest = argv[0], argv[1:]

    # Strip a leading ``--project-dir P`` so it doesn't reach the per-verb
    # argparse which doesn't know about it.
    project_dir: str | None = None
    cleaned: list[str] = []
    skip_next = False
    for i, tok in enumerate(rest):
        if skip_next:
            skip_next = False
            continue
        if tok in ("--project-dir", "-p") and i + 1 < len(rest):
            project_dir = rest[i + 1]
            skip_next = True
            continue
        if tok.startswith("--project-dir="):
            project_dir = tok.split("=", 1)[1]
            continue
        cleaned.append(tok)

    return dispatch(verb_name, cleaned, project_dir=project_dir)


def _print_help() -> None:
    print("usage: camflow ctl [--project-dir DIR] <verb> [args]\n")
    if VERBS:
        print("verbs:")
        width = max(len(n) for n in VERBS)
        for name in list_verb_names():
            spec = VERBS[name]
            tag = (
                "  " if spec.autonomy == AUTONOMY_AUTONOMOUS else "🔒"
            )
            print(f"  {tag} {name.ljust(width)}  {spec.help}")
        print()
        print("🔒 = confirm verb (queued, needs `camflow chat --pending`)")
    else:
        print("(no verbs registered yet)")


def _load_verb_registrations() -> None:
    """Import side-effect modules so they call ``register_verb``.

    Add new verb modules to the list below. Try/except keeps the CLI
    usable when an individual module is removed during refactors.
    """
    # Phase A: read-only verbs are added in a follow-up commit (A5).
    # When that lands, replace this list.
    modules_to_load: list[str] = [
        # "camflow.cli_entry.ctl_read",
    ]
    for mod in modules_to_load:
        try:
            __import__(mod)
        except ImportError:
            pass
