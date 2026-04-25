"""Integration: real-process watchdog loop.

Spawns a short-lived child process that stands in for the engine —
writes a heartbeat with its own pid, then sleeps. We kill it, then
run the watchdog with a stubbed ``restart_engine`` and verify that
the real ``os.kill(pid, 0)`` liveness check + the real heartbeat file
reads make the decision loop trigger exactly the transitions we
expect.

The goal is to cover what the unit tests can't: the decision function
against *actual* OS signals and filesystem state, not hand-crafted dicts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from camflow.engine.monitor import _utcnow_iso, heartbeat_path
from camflow.engine.watchdog import Decision, Watchdog


def _spawn_fake_engine(project_dir: Path) -> subprocess.Popen:
    """Spawn a subprocess that writes a heartbeat and sleeps.

    Uses ``python -c`` so the test doesn't depend on any harness binary.
    The child writes the heartbeat once and then idles — a single write
    is enough to prove the watchdog reads the real file.
    """
    script = textwrap.dedent(f"""
        import json, os, time
        from datetime import datetime, timezone
        hb = {str(heartbeat_path(project_dir))!r}
        os.makedirs(os.path.dirname(hb), exist_ok=True)
        payload = {{
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "pc": "test_node",
            "status": "running",
        }}
        with open(hb, 'w') as f:
            json.dump(payload, f)
        # Idle — test will kill us when it wants the engine "dead".
        while True:
            time.sleep(0.1)
    """)
    return subprocess.Popen([sys.executable, "-c", script])


def _write_state(project_dir: Path, status: str) -> None:
    p = project_dir / ".camflow" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"status": status, "pc": "test_node"}))


class _InstrumentedWatchdog(Watchdog):
    """Watchdog that records restart calls without spawning real engines."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.restart_calls = 0

    def restart_engine(self):
        self.restart_calls += 1
        self.restart_count += 1
        return None


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_real_process_death_triggers_restart(tmp_path):
    """End-to-end: spawn → heartbeat → kill → watchdog detects → restart."""
    child = _spawn_fake_engine(tmp_path)
    try:
        # Wait for the child to actually write its heartbeat.
        hb_file = Path(heartbeat_path(tmp_path))
        assert _wait_for(lambda: hb_file.exists(), timeout=3), \
            "fake engine never wrote heartbeat"
        _write_state(tmp_path, "running")

        # Sanity: tick() sees a HEALTHY engine right now.
        wf = tmp_path / "workflow.yaml"
        wf.write_text("test_node:\n  do: cmd echo ok\n")
        w = _InstrumentedWatchdog(
            str(wf), str(tmp_path),
            poll_interval=1, stale_threshold=60, max_restarts=3,
        )
        assert w.tick() == Decision.HEALTHY
        assert w.restart_calls == 0

        # Now kill the fake engine (SIGKILL — simulates OOM / kernel kill).
        child.kill()
        child.wait(timeout=5)
        # is_process_alive on the dead pid must return False — this is
        # the real integration check.
        assert not _is_alive(child.pid)

        # tick() should now decide RESTART because the heartbeat's pid
        # (the child's) is dead, even though the heartbeat timestamp
        # itself is still fresh.
        assert w.tick() == Decision.RESTART
        assert w.restart_calls == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_state_terminal_halts_watchdog_even_with_dead_engine(tmp_path):
    """If the workflow finished, don't restart — even if pid is dead.

    Protects against the case where the engine writes its terminal
    state.json and then exits: the watchdog must see status=done and
    bow out, not panic and resurrect a completed workflow.
    """
    child = _spawn_fake_engine(tmp_path)
    try:
        hb_file = Path(heartbeat_path(tmp_path))
        assert _wait_for(lambda: hb_file.exists(), timeout=3)
        _write_state(tmp_path, "done")  # workflow already finished
        child.kill()
        child.wait(timeout=5)

        wf = tmp_path / "workflow.yaml"
        wf.write_text("test_node:\n  do: cmd echo ok\n")
        w = _InstrumentedWatchdog(
            str(wf), str(tmp_path),
            poll_interval=1, stale_threshold=60, max_restarts=3,
        )
        assert w.tick() == Decision.EXIT_CLEAN
        assert w.restart_calls == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_run_loop_exits_when_state_turns_terminal(tmp_path):
    """Run the full Watchdog.run() loop: it must exit when state flips.

    Simulates a stop: engine running, watchdog healthy, then state
    becomes interrupted — watchdog should return 0 promptly.
    """
    child = _spawn_fake_engine(tmp_path)
    try:
        hb_file = Path(heartbeat_path(tmp_path))
        assert _wait_for(lambda: hb_file.exists(), timeout=3)
        _write_state(tmp_path, "running")

        wf = tmp_path / "workflow.yaml"
        wf.write_text("test_node:\n  do: cmd echo ok\n")
        w = _InstrumentedWatchdog(
            str(wf), str(tmp_path),
            poll_interval=0.1, stale_threshold=60, max_restarts=3,
            restart_cooldown=0.1,
        )

        result: list[int] = []

        def run_thread():
            result.append(w.run())

        t = threading.Thread(target=run_thread, daemon=True)
        t.start()

        # Give the watchdog a moment to hit its first HEALTHY tick.
        time.sleep(0.3)
        _write_state(tmp_path, "interrupted")  # user ran `camflow stop`

        t.join(timeout=3)
        assert not t.is_alive(), "watchdog did not exit after terminal state"
        assert result == [0]
        assert w.restart_calls == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _is_alive(pid: int) -> bool:
    """Local helper — avoid importing is_process_alive in the test body
    (we want the test to observe the real kernel state, and using the
    same helper the subject uses proves the contract it relies on)."""
    from camflow.engine.monitor import is_process_alive
    return is_process_alive(pid)
