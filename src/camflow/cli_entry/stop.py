"""`camflow stop` subcommand — signal a running engine to exit.

The engine writes its pid to two places on startup: the heartbeat
file (``.camflow/heartbeat.json``) and, when daemonized, the pidfile
(``.camflow/engine.pid``). We read the heartbeat first (it's the
source of truth for a live engine) and fall back to the pidfile so
``stop`` still works after the engine cleaned up the heartbeat
during shutdown.

Behavior:
  * Default — send SIGTERM, which the engine's signal handler
    intercepts to finish the current node and exit cleanly.
  * ``--force`` — send SIGKILL (no cleanup). Reserved for runaway
    engines that ignored SIGTERM.
  * After sending the signal, poll for up to ``--timeout`` seconds
    (default 10) waiting for the pid to disappear; report
    success/failure accordingly.

Exit codes:
  0 — engine exited (or was already gone).
  1 — signal sent but pid still alive after the timeout.
  2 — no engine found to stop.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from camflow.engine.monitor import (
    heartbeat_path,
    is_process_alive,
    load_heartbeat,
    lock_path,
)
from camflow.engine.watchdog import (
    watchdog_lock_path,
    watchdog_pid_path,
)


ENGINE_PIDFILE = "engine.pid"
DEFAULT_STOP_TIMEOUT = 10  # seconds


def _resolve_project_dir(project_dir: str | None) -> str:
    """Default to the current working directory if not supplied."""
    return project_dir or os.getcwd()


def _read_pidfile(project_dir: str) -> int | None:
    path = os.path.join(project_dir, ".camflow", ENGINE_PIDFILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return int(content) if content else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _find_engine_pid(project_dir: str) -> tuple[int | None, str]:
    """Return (pid, source) for the running engine.

    source ∈ {"heartbeat", "pidfile", ""} — purely for the status
    message. We prefer the heartbeat because it carries a recent
    timestamp; the pidfile is older and only written by --daemon.
    """
    hb = load_heartbeat(heartbeat_path(project_dir))
    if hb:
        pid = hb.get("pid")
        if isinstance(pid, int):
            return pid, "heartbeat"
    pid = _read_pidfile(project_dir)
    if pid:
        return pid, "pidfile"
    return None, ""


def _wait_for_exit(pid: int, timeout: int, poll: float = 0.2) -> bool:
    """Poll until ``pid`` is gone or the timeout expires. Returns True
    if the process exited in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(poll)
    return not is_process_alive(pid)


def _cleanup_leftover_files(project_dir: str) -> None:
    """Best-effort remove the lock + heartbeat + pidfile after we've
    confirmed the engine is gone. Stale files here confuse `camflow
    status` and `camflow run` guards."""
    candidates = [
        lock_path(project_dir),
        heartbeat_path(project_dir),
        os.path.join(project_dir, ".camflow", ENGINE_PIDFILE),
        watchdog_lock_path(project_dir),
        watchdog_pid_path(project_dir),
    ]
    for path in candidates:
        try:
            os.remove(path)
        except OSError:
            pass


def _read_watchdog_pid(project_dir: str) -> int | None:
    path = watchdog_pid_path(project_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return int(content) if content else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _stop_watchdog(project_dir: str, timeout: int) -> None:
    """Signal the sibling watchdog BEFORE the engine so it doesn't
    trigger a restart of the engine we're about to stop.

    Best-effort: if the watchdog is already gone, move on.
    """
    pid = _read_watchdog_pid(project_dir)
    if pid is None or not is_process_alive(pid):
        return
    print(f"Stopping watchdog pid {pid} first...")
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    _wait_for_exit(pid, timeout)


def stop_command(args) -> int:
    project_dir = _resolve_project_dir(args.project_dir)

    # Always stop the watchdog first (if any) so it doesn't observe
    # the engine dying and trigger a restart behind our back.
    _stop_watchdog(project_dir, args.timeout)

    pid, source = _find_engine_pid(project_dir)

    if pid is None:
        print("No engine pid found in .camflow/heartbeat.json or "
              ".camflow/engine.pid — nothing to stop.", file=sys.stderr)
        return 2

    if not is_process_alive(pid):
        print(f"Engine pid {pid} (from {source}) is not running; "
              f"cleaning up stale files.")
        _cleanup_leftover_files(project_dir)
        return 0

    sig = signal.SIGKILL if args.force else signal.SIGTERM
    sig_name = "SIGKILL" if args.force else "SIGTERM"
    print(f"Sending {sig_name} to engine pid {pid} (from {source})...")

    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # Raced with engine exit — treat as success.
        print(f"Engine pid {pid} exited before signal could be delivered.")
        _cleanup_leftover_files(project_dir)
        return 0
    except PermissionError:
        print(f"ERROR: no permission to signal pid {pid} "
              f"(owned by another user?)", file=sys.stderr)
        return 1

    if _wait_for_exit(pid, args.timeout):
        print(f"Engine pid {pid} exited.")
        _cleanup_leftover_files(project_dir)
        return 0

    print(
        f"ERROR: pid {pid} still alive after {args.timeout}s. "
        f"Retry with --force to send SIGKILL.",
        file=sys.stderr,
    )
    return 1


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow stop")
        p = parser
    else:
        p = subparsers.add_parser(
            "stop",
            help="Signal a running camflow engine to exit",
        )
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd). .camflow/ lives here.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Send SIGKILL instead of SIGTERM (no cleanup).",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait for the process to exit (default: 10).",
    )
    p.set_defaults(func=stop_command)
    if subparsers is None:
        return parser
    return p


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
