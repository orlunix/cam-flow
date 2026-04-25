"""Watchdog process: auto-restart the engine after a silent crash.

``camflow run --daemon`` leaves the engine as a daemon. If that engine
dies on OOM, a kernel kill, or a segfault that escapes the signal
handler, the workflow stalls forever — no one restarts it. The
watchdog is a *sibling* process (not a child, so the engine's death
doesn't cascade) that polls engine liveness and runs ``camflow resume``
on a genuine crash.

Architecture::

    camflow run --daemon workflow.yaml
        │
        ├── fork #1 — engine daemon (camflow main loop)
        │
        └── fork #2 — watchdog daemon (this module's main loop)

The watchdog's decision loop is the only interesting thing here; the
fork plumbing lives in ``cli_entry/main.py``.

Decision rules (checked every ``poll_interval`` seconds):

* ``state.status`` ∈ {done, failed, interrupted, aborted, engine_error}
  → the workflow has reached a terminal state. Exit cleanly. Do NOT
  try to restart — the user stopped it, or it really did fail.
* Heartbeat fresh (<``stale_threshold`` seconds) AND engine pid alive
  → HEALTHY. Sleep and poll again.
* Heartbeat stale OR engine pid dead → the engine is gone. Increment
  the restart counter:
  * If ``restart_count < max_restarts`` → spawn ``camflow resume
    --daemon`` and sleep past ``restart_cooldown`` to let the new
    engine write its first heartbeat.
  * Else → EXHAUSTED. Log and exit non-zero. Manual intervention.

The counter is in-memory only. A watchdog that itself gets restarted
starts fresh — matches the intuition that the operator is now
involved.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from camflow.backend.persistence import load_state
from camflow.engine.monitor import (
    DEFAULT_STALE_THRESHOLD,
    heartbeat_path,
    is_process_alive,
    is_stale,
    load_heartbeat,
)


WATCHDOG_LOCK_FILENAME = "watchdog.lock"
WATCHDOG_PID_FILENAME = "watchdog.pid"
WATCHDOG_LOG_FILENAME = "watchdog.log"

DEFAULT_POLL_INTERVAL = 30        # seconds between health checks
DEFAULT_MAX_RESTARTS = 3          # give up after N auto-restarts
DEFAULT_RESTART_COOLDOWN = 45     # seconds after spawning resume before next poll
# 60s is the user-facing stale threshold for the watchdog; the engine
# itself uses 120s (DEFAULT_STALE_THRESHOLD) for general status. The
# watchdog is more aggressive — a truly hung engine shouldn't wait
# through two missed windows.
DEFAULT_WATCHDOG_STALE_THRESHOLD = 60


# Terminal statuses the engine can land in — either by normal
# completion, explicit user action, or internal error classification.
# If we observe one of these, the user either got what they wanted or
# needs to look at it by hand. Either way, NOT a watchdog job.
TERMINAL_STATUSES = frozenset(
    {"done", "failed", "interrupted", "aborted", "engine_error"}
)


# ---- paths ---------------------------------------------------------------


def watchdog_lock_path(project_dir: str | os.PathLike) -> str:
    return os.path.join(str(project_dir), ".camflow", WATCHDOG_LOCK_FILENAME)


def watchdog_pid_path(project_dir: str | os.PathLike) -> str:
    return os.path.join(str(project_dir), ".camflow", WATCHDOG_PID_FILENAME)


def watchdog_log_path(project_dir: str | os.PathLike) -> str:
    return os.path.join(str(project_dir), ".camflow", WATCHDOG_LOG_FILENAME)


# ---- lock ---------------------------------------------------------------


class WatchdogLockError(RuntimeError):
    def __init__(self, path: str, holder_pid: int | None):
        self.path = path
        self.holder_pid = holder_pid
        msg = "another watchdog is already running on this workflow"
        if holder_pid:
            msg += f" (pid {holder_pid})"
        msg += f"; lock at {path}"
        super().__init__(msg)


class WatchdogLock:
    """flock-based single-writer lock for the watchdog process.

    Same shape as :class:`camflow.engine.monitor.EngineLock` but with
    its own file so an engine lock does not block a watchdog (they're
    supposed to run simultaneously).
    """

    def __init__(self, project_dir: str):
        self.project_dir = str(project_dir)
        self.path = watchdog_lock_path(project_dir)
        self._fd = None

    def acquire(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._read_pid(fd)
            fd.close()
            # Simple stale-pid cleanup: if the holder pid is dead we
            # can safely steal. (Stricter rules from EngineLock aren't
            # worth it here — watchdogs have no shared state to race.)
            if holder and not is_process_alive(holder):
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                return self.acquire()
            raise WatchdogLockError(self.path, holder) from None
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        os.fsync(fd.fileno())
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fd.close()
        finally:
            try:
                os.remove(self.path)
            except OSError:
                pass

    @staticmethod
    def _read_pid(fd) -> int | None:
        try:
            fd.seek(0)
            content = fd.read().strip()
            return int(content) if content else None
        except (ValueError, OSError):
            return None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


# ---- decision ------------------------------------------------------------


class Decision:
    """Enum-ish: what the watchdog decided this tick."""
    HEALTHY = "healthy"
    RESTART = "restart"
    EXIT_CLEAN = "exit_clean"
    EXHAUSTED = "exhausted"


def decide(
    state: dict | None,
    heartbeat: dict | None,
    restart_count: int,
    max_restarts: int,
    stale_threshold: int = DEFAULT_WATCHDOG_STALE_THRESHOLD,
) -> tuple[str, str]:
    """Pure decision function — returns (decision, reason).

    Kept module-level and side-effect-free so tests can drive it with
    handcrafted state/heartbeat dicts and not worry about files or time.
    """
    if state:
        status = state.get("status")
        if status in TERMINAL_STATUSES:
            return Decision.EXIT_CLEAN, f"state.status={status!r}"
    # No state yet → treat like a pending startup; wait another tick.
    if not state:
        return Decision.HEALTHY, "state.json not yet present"

    pid = heartbeat.get("pid") if heartbeat else None
    alive = is_process_alive(pid)
    stale = is_stale(heartbeat, threshold=stale_threshold)

    if not stale and alive:
        return Decision.HEALTHY, f"heartbeat fresh, pid {pid} alive"

    # Engine is gone (crash, kill -9, hung past stale window).
    if restart_count >= max_restarts:
        return (
            Decision.EXHAUSTED,
            f"engine dead and restart_count={restart_count} >= max={max_restarts}",
        )
    why = []
    if stale:
        why.append("heartbeat stale")
    if not alive:
        why.append(f"pid {pid} not alive")
    return Decision.RESTART, ", ".join(why) or "engine not healthy"


# ---- watchdog main --------------------------------------------------------


class Watchdog:
    """Polling watchdog — spawn-resume on crash, exit on terminal status.

    Usage::

        Watchdog(workflow_path, project_dir).run()

    ``run()`` blocks until the engine reaches a terminal status, the
    watchdog receives SIGTERM/SIGINT, or the restart budget is spent.
    """

    def __init__(
        self,
        workflow_path: str,
        project_dir: str,
        *,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        stale_threshold: int = DEFAULT_WATCHDOG_STALE_THRESHOLD,
        restart_cooldown: int = DEFAULT_RESTART_COOLDOWN,
        camflow_bin: str | None = None,
        logger: logging.Logger | None = None,
    ):
        self.workflow_path = os.path.abspath(workflow_path)
        self.project_dir = os.path.abspath(project_dir)
        self.poll_interval = poll_interval
        self.max_restarts = max_restarts
        self.stale_threshold = stale_threshold
        self.restart_cooldown = restart_cooldown
        # Default to "camflow" on PATH. Tests/ops can override.
        self.camflow_bin = camflow_bin or os.environ.get("CAMFLOW_BIN") or "camflow"
        self.restart_count = 0
        self._stop = threading.Event()
        self.log = logger or logging.getLogger("camflow.watchdog")

    # ---- decision inputs (one helper per file for easy mocking) ----

    def _load_state(self) -> dict | None:
        return load_state(os.path.join(self.project_dir, ".camflow", "state.json"))

    def _load_heartbeat(self) -> dict | None:
        return load_heartbeat(heartbeat_path(self.project_dir))

    # ---- restart --------------------------------------------------

    def restart_engine(self) -> subprocess.Popen | None:
        """Spawn ``camflow resume --daemon``; return the Popen (child exits
        immediately after daemon fork, so this is mostly for tests to
        wait on). Returns None if the spawn itself failed."""
        cmd = [
            self.camflow_bin,
            "resume",
            "--daemon",
            self.workflow_path,
            "--project-dir",
            self.project_dir,
        ]
        self.log.info("restart #%d: %s", self.restart_count + 1, " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as e:
            self.log.error("restart spawn failed: %s", e)
            return None
        # Don't wait indefinitely — the parent-of-daemon should exit
        # promptly. A short bounded wait catches catastrophic failures
        # (e.g. camflow binary missing) without blocking the poll loop.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Resume is taking unusually long to double-fork; let it
            # be — we'll detect engine liveness via heartbeat anyway.
            pass
        self.restart_count += 1
        return proc

    # ---- lifecycle ------------------------------------------------

    def _install_signal_handlers(self) -> None:
        # signal.signal() only works from the main thread. When the
        # watchdog is driven programmatically from a worker thread
        # (tests, embedding scenarios) we just skip — the caller can
        # set self._stop directly to request shutdown.
        if threading.current_thread() is not threading.main_thread():
            return
        def _handler(signum, _frame):
            self.log.info("received signal %d, exiting", signum)
            self._stop.set()
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _write_pidfile(self) -> None:
        path = watchdog_pid_path(self.project_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        os.rename(tmp, path)

    def _cleanup_pidfile(self) -> None:
        try:
            os.remove(watchdog_pid_path(self.project_dir))
        except OSError:
            pass

    def tick(self) -> str:
        """Run one decision iteration without sleeping. Returns the decision.

        Exposed for unit tests that want to step the loop deterministically.
        """
        state = self._load_state()
        heartbeat = self._load_heartbeat()
        decision, reason = decide(
            state, heartbeat, self.restart_count,
            self.max_restarts, self.stale_threshold,
        )
        self.log.debug("decision=%s reason=%s restart_count=%d",
                       decision, reason, self.restart_count)
        if decision == Decision.RESTART:
            self.log.warning("engine down (%s) — triggering restart", reason)
            self.restart_engine()
        elif decision == Decision.EXHAUSTED:
            self.log.error(
                "restart budget exhausted (%d/%d). Giving up; manual "
                "intervention needed.", self.restart_count, self.max_restarts,
            )
        elif decision == Decision.EXIT_CLEAN:
            self.log.info("engine reached terminal status (%s); exiting", reason)
        return decision

    def run(self) -> int:
        """Main loop. Returns shell exit code: 0 clean, 2 exhausted."""
        self._install_signal_handlers()
        try:
            with WatchdogLock(self.project_dir):
                self._write_pidfile()
                try:
                    return self._run_loop()
                finally:
                    self._cleanup_pidfile()
        except WatchdogLockError as e:
            self.log.error("cannot acquire watchdog lock: %s", e)
            return 1

    def _run_loop(self) -> int:
        self.log.info(
            "watchdog started: workflow=%s poll=%ds max_restarts=%d",
            self.workflow_path, self.poll_interval, self.max_restarts,
        )
        while not self._stop.is_set():
            try:
                decision = self.tick()
            except Exception:
                # Never let a transient exception kill the watchdog —
                # log it and keep polling.
                self.log.exception("tick failed; continuing")
                decision = Decision.HEALTHY
            if decision == Decision.EXIT_CLEAN:
                return 0
            if decision == Decision.EXHAUSTED:
                return 2
            # Cooldown a bit longer after spawning a restart so the new
            # engine has time to write its first heartbeat before we
            # observe staleness again.
            wait = self.restart_cooldown if decision == Decision.RESTART else self.poll_interval
            self._stop.wait(wait)
        return 0


# ---- CLI entry -----------------------------------------------------------


def _configure_logger(project_dir: str, verbose: bool) -> logging.Logger:
    log = logging.getLogger("camflow.watchdog")
    # Always reconfigure so repeat invocations in the same process (tests)
    # don't accumulate handlers.
    for h in list(log.handlers):
        log.removeHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # File handler — persistent event log the operator (and status) can read.
    try:
        fh = logging.FileHandler(watchdog_log_path(project_dir))
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    # Stderr handler — harmless when daemonized (stderr → /dev/null) and
    # useful when running in the foreground.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


def watchdog_command(args) -> int:
    if not os.path.isfile(args.workflow):
        print(f"ERROR: workflow file not found: {args.workflow}", file=sys.stderr)
        return 1
    project_dir = args.project_dir or os.path.dirname(os.path.abspath(args.workflow)) or "."
    project_dir = os.path.abspath(project_dir)
    Path(os.path.join(project_dir, ".camflow")).mkdir(parents=True, exist_ok=True)

    log = _configure_logger(project_dir, args.verbose)
    wd = Watchdog(
        workflow_path=args.workflow,
        project_dir=project_dir,
        poll_interval=args.poll_interval,
        max_restarts=args.max_restarts,
        stale_threshold=args.stale_threshold,
        restart_cooldown=args.restart_cooldown,
        logger=log,
    )
    return wd.run()


def build_parser(subparsers=None):
    if subparsers is None:
        parser = argparse.ArgumentParser(prog="camflow watchdog")
        p = parser
    else:
        p = subparsers.add_parser(
            "watchdog",
            help="Run the watchdog loop against an existing workflow",
        )
    p.add_argument("workflow", help="Path to workflow YAML")
    p.add_argument("--project-dir", "-p", default=None)
    p.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
        help="Seconds between health checks (default: %(default)s)",
    )
    p.add_argument(
        "--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS,
        help="Give up after this many auto-restarts (default: %(default)s)",
    )
    p.add_argument(
        "--stale-threshold", type=int, default=DEFAULT_WATCHDOG_STALE_THRESHOLD,
        help="Seconds of missed heartbeat before considering the engine "
             "hung (default: %(default)s)",
    )
    p.add_argument(
        "--restart-cooldown", type=int, default=DEFAULT_RESTART_COOLDOWN,
        help="Seconds to wait after spawning resume before polling again "
             "(default: %(default)s)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=watchdog_command)
    if subparsers is None:
        return parser
    return p


def main(argv=None):
    parser = build_parser(None)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
