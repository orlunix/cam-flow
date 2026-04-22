"""Engine self-monitoring: heartbeat thread, stale detection, lock file.

camflow is a USER of camc — the engine manages its own lifecycle. This
module provides three primitives Engine.run() composes:

    HeartbeatThread  — daemon that periodically stamps .camflow/heartbeat.json
    EngineLock       — fcntl-based single-writer guard on .camflow/engine.lock
    is_stale / is_process_alive / load_heartbeat — recovery-time checks

Layout of .camflow/heartbeat.json::

    {
        "pid": 12345,
        "timestamp": "2026-04-21T12:00:00Z",
        "pc": "eval_swerv",
        "iteration": 3,
        "agent_id": "5130c656",
        "uptime_seconds": 1234
    }

A heartbeat is "stale" when (now - timestamp) exceeds a threshold; a
stale heartbeat on a non-existent PID means the previous engine died.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


HEARTBEAT_FILENAME = "heartbeat.json"
LOCK_FILENAME = "engine.lock"

DEFAULT_HEARTBEAT_INTERVAL = 30   # seconds between writes
DEFAULT_STALE_THRESHOLD = 120     # seconds after which heartbeat is "stale"


# ---- time helpers --------------------------------------------------------


def _utcnow_iso() -> str:
    """ISO-8601 UTC with trailing 'Z' — the format heartbeat.json uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> float | None:
    """Parse the ISO-8601 UTC format we write; return epoch seconds.

    Returns None if the string is malformed (treat malformed as stale).
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        s = ts.rstrip("Z")
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# ---- process liveness ----------------------------------------------------


def is_process_alive(pid: int | None) -> bool:
    """True if a process with `pid` currently exists on this host.

    Uses `os.kill(pid, 0)` which is a no-op signal that raises
    ``ProcessLookupError`` if the pid doesn't exist and
    ``PermissionError`` if the pid exists but is owned by another user
    (still alive from our POV). Returns False on any other error.
    """
    if not pid or not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        return False
    return True


# ---- heartbeat I/O -------------------------------------------------------


def heartbeat_path(project_dir: str | os.PathLike) -> str:
    return os.path.join(str(project_dir), ".camflow", HEARTBEAT_FILENAME)


def lock_path(project_dir: str | os.PathLike) -> str:
    return os.path.join(str(project_dir), ".camflow", LOCK_FILENAME)


def write_heartbeat(path: str, payload: dict) -> None:
    """Atomically rewrite heartbeat.json (tmp + rename)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def load_heartbeat(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_stale(heartbeat: dict | None, threshold: int = DEFAULT_STALE_THRESHOLD,
             now: float | None = None) -> bool:
    """True if the heartbeat is older than `threshold` seconds.

    A missing heartbeat (None) is considered stale — if the engine was
    running long enough to care about recovery, it had time to write
    at least one.
    """
    if heartbeat is None:
        return True
    ts = _parse_iso(heartbeat.get("timestamp", ""))
    if ts is None:
        return True
    current = now if now is not None else time.time()
    return (current - ts) > threshold


# ---- heartbeat thread ----------------------------------------------------


class HeartbeatThread(threading.Thread):
    """Daemon thread that periodically writes heartbeat.json.

    The engine passes a ``state_getter`` callable so we always snapshot
    the *current* pc / iteration / agent_id rather than a stale copy.
    The thread exits when ``stop()`` is called (or when the interpreter
    shuts down, since it's a daemon).
    """

    def __init__(
        self,
        project_dir: str,
        state_getter,
        interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        workflow_path: str | None = None,
    ):
        super().__init__(name="camflow-heartbeat", daemon=True)
        self.project_dir = project_dir
        self.state_getter = state_getter
        self.interval = interval
        self.workflow_path = workflow_path
        self._stop_event = threading.Event()
        self._started_at = time.time()
        self.path = heartbeat_path(project_dir)

    def _snapshot(self) -> dict:
        state = self.state_getter() or {}
        snap = {
            "pid": os.getpid(),
            "timestamp": _utcnow_iso(),
            "pc": state.get("pc"),
            "iteration": state.get("iteration"),
            "agent_id": state.get("current_agent_id"),
            "status": state.get("status"),
            "started_at": self._started_at,
            "uptime_seconds": int(time.time() - self._started_at),
        }
        if self.workflow_path:
            snap["workflow_path"] = self.workflow_path
        # Track when the current agent started so `camflow status` can
        # report how long the agent has been running.
        agent_started = state.get("current_node_started_at")
        if agent_started:
            snap["agent_started_at"] = agent_started
        return snap

    def write_once(self) -> None:
        """Write one heartbeat immediately. Safe to call from any thread."""
        try:
            write_heartbeat(self.path, self._snapshot())
        except OSError:
            # A heartbeat write failure is never fatal — we'll try again
            # on the next tick. Swallow so we don't take down the engine.
            pass

    def run(self):
        # Emit an initial heartbeat so `camflow status` works the moment
        # the engine is alive, without waiting a full interval.
        self.write_once()
        while not self._stop_event.wait(self.interval):
            self.write_once()

    def stop(self, remove_file: bool = True) -> None:
        self._stop_event.set()
        if remove_file:
            try:
                os.remove(self.path)
            except OSError:
                pass


# ---- engine lock ---------------------------------------------------------


class EngineLockError(RuntimeError):
    """Raised when the engine lock is already held by another process."""

    def __init__(self, path: str, holder_pid: int | None):
        self.path = path
        self.holder_pid = holder_pid
        msg = f"another engine is already running on this workflow"
        if holder_pid:
            msg += f" (pid {holder_pid})"
        msg += f"; lock at {path}"
        super().__init__(msg)


class EngineLock:
    """Exclusive file lock on .camflow/engine.lock (flock-based).

    Use as a context manager::

        with EngineLock(project_dir) as lock:
            ...                           # engine runs
        # lock released on exit

    If another process already holds the lock, ``__enter__`` raises
    :class:`EngineLockError` whose ``holder_pid`` attribute carries the
    pid recorded in the lock file (best-effort — may be ``None`` if the
    other process hadn't yet written its pid).
    """

    def __init__(self, project_dir: str):
        self.path = lock_path(project_dir)
        self._fd = None

    def acquire(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Open rw so we can both flock and rewrite the pid. Using "a+"
        # avoids truncating the file before we know we own the lock
        # (another process may still hold it).
        fd = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._read_pid(fd)
            fd.close()
            raise EngineLockError(self.path, holder) from None
        # Truncate + write our pid now that we own the lock.
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
