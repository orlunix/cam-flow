"""Shared daemonize + watchdog-spawn helpers used by ``run`` and ``resume``.

``daemonize_engine`` does a minimal POSIX double-fork so the engine
becomes a session leader detached from the controlling TTY. Parent
returns True (caller should exit 0); child returns False and keeps
running as the engine.

``spawn_watchdog`` Popens ``camflow watchdog`` in a new session so its
lifetime is independent of the engine — that is the whole point of a
watchdog. The watchdog itself holds a flock that prevents duplicate
instances, so re-invocations (e.g. a resume running while a previous
watchdog is still up) cost at most one no-op process.
"""

from __future__ import annotations

import os
import subprocess
import sys


ENGINE_PIDFILE = "engine.pid"
ENGINE_LOGFILE = "engine.log"


def daemonize_engine(project_dir: str) -> bool:
    """Detach the engine to run in the background.

    Double-fork pattern so the grandchild is reparented to init; parent
    returns True after printing the grandchild pid; grandchild returns
    False with stdio redirected to ``.camflow/engine.log``.
    """
    camflow_dir = os.path.join(project_dir, ".camflow")
    os.makedirs(camflow_dir, exist_ok=True)
    log_path = os.path.join(camflow_dir, ENGINE_LOGFILE)
    pid_path = os.path.join(camflow_dir, ENGINE_PIDFILE)

    pid = os.fork()
    if pid > 0:
        print(f"camflow daemon started (pid {pid}); logs at {log_path}")
        return True

    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.chdir(project_dir)
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    log_fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    return False


def _camflow_bin() -> str:
    """Resolve the camflow CLI binary to re-invoke for the watchdog.

    Priority:
      1. ``CAMFLOW_BIN`` env var — set by tests or custom installs.
      2. ``sys.argv[0]`` when it looks like an absolute path or a script
         (lets the watchdog match however the parent was launched —
         e.g. ``~/.cam/camflow`` wrappers vs ``python -m``).
      3. Fallback to ``camflow`` on PATH.
    """
    env = os.environ.get("CAMFLOW_BIN")
    if env:
        return env
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and (os.path.isabs(argv0) or argv0.startswith(".")):
        if os.path.exists(argv0):
            return argv0
    return "camflow"


def spawn_watchdog(
    workflow_path: str,
    project_dir: str,
    *,
    poll_interval: int | None = None,
    max_restarts: int | None = None,
    stale_threshold: int | None = None,
    restart_cooldown: int | None = None,
) -> subprocess.Popen | None:
    """Launch ``camflow watchdog`` as a detached sibling process.

    Returns the Popen handle (mostly for tests) or None on spawn
    failure. The parent does not wait for it — the watchdog runs
    until the engine reaches a terminal status or exhausts restarts.
    """
    cmd = [
        _camflow_bin(),
        "watchdog",
        os.path.abspath(workflow_path),
        "--project-dir",
        os.path.abspath(project_dir),
    ]
    if poll_interval is not None:
        cmd += ["--poll-interval", str(poll_interval)]
    if max_restarts is not None:
        cmd += ["--max-restarts", str(max_restarts)]
    if stale_threshold is not None:
        cmd += ["--stale-threshold", str(stale_threshold)]
    if restart_cooldown is not None:
        cmd += ["--restart-cooldown", str(restart_cooldown)]

    log_path = os.path.join(project_dir, ".camflow", "watchdog.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        log_fd = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fd if log_fd is not None else subprocess.DEVNULL,
            stderr=log_fd if log_fd is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        print(f"WARNING: failed to spawn watchdog: {e}", file=sys.stderr)
        if log_fd is not None:
            os.close(log_fd)
        return None
    finally:
        if log_fd is not None:
            try:
                os.close(log_fd)
            except OSError:
                pass
    return proc
