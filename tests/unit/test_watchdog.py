"""Unit tests for camflow.engine.watchdog.

The decision function is pure — we drive it with hand-built state and
heartbeat dicts. The Watchdog class is tested by stubbing out
``restart_engine`` and the two ``_load_*`` helpers so tick() runs
deterministically without any real subprocess or filesystem churn.

What we cover:

* Decision matrix: terminal statuses, healthy engine, dead engine under
  restart budget, dead engine past restart budget, hung engine
  (heartbeat stale while pid still alive).
* Watchdog.tick wiring: confirms the right method is invoked per
  decision and that restart_count increments only on successful restart.
* WatchdogLock: acquire/release, second acquirer blocks with the right
  pid, stale-pid cleanup on re-acquire, pidfile written + cleaned by
  run().
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

import pytest

from camflow.engine.monitor import _utcnow_iso, heartbeat_path, write_heartbeat
from camflow.engine.watchdog import (
    DEFAULT_MAX_RESTARTS,
    Decision,
    Watchdog,
    WatchdogLock,
    WatchdogLockError,
    decide,
    watchdog_lock_path,
    watchdog_pid_path,
)

DEAD_PID = 4194301  # implausible pid — same convention as test_monitor


# ---- decision function --------------------------------------------------


class TestDecide:
    def test_no_state_yields_healthy(self):
        d, _ = decide(state=None, heartbeat=None,
                      restart_count=0, max_restarts=3)
        assert d == Decision.HEALTHY

    @pytest.mark.parametrize("terminal", list({
        "done", "failed", "interrupted", "aborted", "engine_error",
    }))
    def test_terminal_status_yields_exit_clean(self, terminal):
        d, _ = decide(
            state={"status": terminal},
            heartbeat={"pid": os.getpid(), "timestamp": _utcnow_iso()},
            restart_count=0, max_restarts=3,
        )
        assert d == Decision.EXIT_CLEAN

    def test_alive_and_fresh_is_healthy(self):
        d, _ = decide(
            state={"status": "running"},
            heartbeat={"pid": os.getpid(), "timestamp": _utcnow_iso()},
            restart_count=0, max_restarts=3,
        )
        assert d == Decision.HEALTHY

    def test_engine_pid_dead_triggers_restart(self):
        d, reason = decide(
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            restart_count=0, max_restarts=3,
        )
        assert d == Decision.RESTART
        assert "not alive" in reason

    def test_stale_heartbeat_triggers_restart_even_when_pid_alive(self):
        # This is the "hung engine" case: process is still there but
        # hasn't written a heartbeat in a while.
        d, reason = decide(
            state={"status": "running"},
            heartbeat={"pid": os.getpid(), "timestamp": "2020-01-01T00:00:00Z"},
            restart_count=0, max_restarts=3,
        )
        assert d == Decision.RESTART
        assert "stale" in reason

    def test_restart_budget_exhausted(self):
        d, reason = decide(
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            restart_count=3, max_restarts=3,
        )
        assert d == Decision.EXHAUSTED
        assert "3" in reason

    def test_missing_heartbeat_with_running_state_triggers_restart(self):
        # If the heartbeat file disappeared entirely, treat that as the
        # engine being gone.
        d, _ = decide(
            state={"status": "running"},
            heartbeat=None,
            restart_count=0, max_restarts=3,
        )
        assert d == Decision.RESTART


# ---- Watchdog.tick wiring ----------------------------------------------


class _StubWatchdog(Watchdog):
    """Watchdog subclass that swaps I/O for test-controlled values."""

    def __init__(self, tmp_path, state, heartbeat, **kwargs):
        # A throwaway workflow file path is fine — tick() never reads it.
        wf = tmp_path / "workflow.yaml"
        wf.write_text("placeholder: {}\n")
        super().__init__(str(wf), str(tmp_path), **kwargs)
        self._state = state
        self._heartbeat = heartbeat
        self.restart_calls = 0

    def _load_state(self):
        return self._state

    def _load_heartbeat(self):
        return self._heartbeat

    def restart_engine(self):
        self.restart_calls += 1
        self.restart_count += 1
        return None


class TestTick:
    def test_healthy_does_not_restart(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "running"},
            heartbeat={"pid": os.getpid(), "timestamp": _utcnow_iso()},
        )
        assert w.tick() == Decision.HEALTHY
        assert w.restart_calls == 0

    def test_terminal_status_does_not_restart(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "done"},
            heartbeat={"pid": os.getpid(), "timestamp": _utcnow_iso()},
        )
        assert w.tick() == Decision.EXIT_CLEAN
        assert w.restart_calls == 0

    def test_dead_engine_triggers_restart(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            max_restarts=3,
        )
        assert w.tick() == Decision.RESTART
        assert w.restart_calls == 1
        assert w.restart_count == 1

    def test_exhausted_does_not_call_restart(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            max_restarts=1,
        )
        w.restart_count = 1  # already at budget
        assert w.tick() == Decision.EXHAUSTED
        assert w.restart_calls == 0

    def test_restart_count_increments_up_to_max(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            max_restarts=2,
        )
        assert w.tick() == Decision.RESTART
        assert w.tick() == Decision.RESTART
        assert w.tick() == Decision.EXHAUSTED
        assert w.restart_calls == 2


# ---- WatchdogLock ------------------------------------------------------


class TestWatchdogLock:
    def test_acquire_and_release(self, tmp_path):
        with WatchdogLock(str(tmp_path)) as lock:
            with open(lock.path) as f:
                assert int(f.read().strip()) == os.getpid()
        assert not os.path.exists(watchdog_lock_path(tmp_path))

    def test_second_acquirer_blocks(self, tmp_path):
        first = WatchdogLock(str(tmp_path))
        first.acquire()
        try:
            second = WatchdogLock(str(tmp_path))
            with pytest.raises(WatchdogLockError) as exc:
                second.acquire()
            assert exc.value.holder_pid == os.getpid()
        finally:
            first.release()

    def test_stale_pid_is_auto_cleaned(self, tmp_path):
        # Simulate a prior watchdog that died without releasing the lock
        # file. The pidfile records a dead pid but no flock is actually
        # held — so the next acquire() should just overwrite cleanly.
        p = watchdog_lock_path(tmp_path)
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(str(DEAD_PID))
        # No flock held — this is the common "file got left behind" case.
        with WatchdogLock(str(tmp_path)) as lock:
            with open(lock.path) as f:
                assert int(f.read().strip()) == os.getpid()


# ---- Watchdog.run lifecycle -------------------------------------------


class _FastExitWatchdog(_StubWatchdog):
    """Stub that declares the workflow terminal immediately.

    Used to drive run() end-to-end without blocking on poll_interval.
    """

    def __init__(self, tmp_path, **kwargs):
        super().__init__(
            tmp_path,
            state={"status": "done"},
            heartbeat={"pid": os.getpid(), "timestamp": _utcnow_iso()},
            **kwargs,
        )


class TestRun:
    def test_run_writes_and_removes_pidfile(self, tmp_path):
        w = _FastExitWatchdog(tmp_path, poll_interval=60)
        rc = w.run()
        assert rc == 0
        # After clean exit the pidfile should be gone.
        assert not os.path.exists(watchdog_pid_path(tmp_path))

    def test_run_refuses_if_lock_already_held(self, tmp_path):
        first = WatchdogLock(str(tmp_path))
        first.acquire()
        try:
            w = _FastExitWatchdog(tmp_path, poll_interval=60)
            rc = w.run()
            assert rc == 1
        finally:
            first.release()

    def test_run_returns_2_when_exhausted(self, tmp_path):
        w = _StubWatchdog(
            tmp_path,
            state={"status": "running"},
            heartbeat={"pid": DEAD_PID, "timestamp": _utcnow_iso()},
            max_restarts=0,
            poll_interval=60,
        )
        rc = w.run()
        assert rc == 2
