"""Unit tests for engine crash-recovery + idempotent `camflow run`.

Covers:
  * Engine._check_and_recover:
      - terminal-but-resumable statuses flip to running with budgets reset
      - status=="done" short-circuits via the "already_done" sentinel
      - status=="running" + stale heartbeat + dead pid → resumed_crash
      - status=="running" + fresh heartbeat → None (lock will stop dupes)
  * Engine.run() short-circuits cleanly when the workflow is already done.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.backend.persistence import save_state_atomic
from camflow.engine.monitor import _utcnow_iso, heartbeat_path, write_heartbeat


# ---- fixtures ----------------------------------------------------------


def _wf_yaml(tmp_path):
    """Write a trivial two-node workflow and return its path."""
    body = (
        "build:\n"
        "  do: shell true\n"
        "  next: verify\n"
        "verify:\n"
        "  do: shell true\n"
    )
    p = tmp_path / "workflow.yaml"
    p.write_text(body)
    return str(p)


def _write_state(tmp_path, state_dict):
    state_dir = tmp_path / ".camflow"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    save_state_atomic(str(path), state_dict)
    return str(path)


def _engine(tmp_path, cfg=None):
    wf = _wf_yaml(tmp_path)
    eng = Engine(wf, str(tmp_path), cfg or EngineConfig())
    eng._load_workflow()
    eng._load_or_init_state()
    return eng


# ---- _check_and_recover -----------------------------------------------


class TestCheckAndRecover:
    def test_done_returns_already_done(self, tmp_path):
        _write_state(tmp_path, {"pc": "verify", "status": "done"})
        eng = _engine(tmp_path)
        assert eng._check_and_recover() == "already_done"

    @pytest.mark.parametrize(
        "status", ["failed", "interrupted", "engine_error", "aborted"],
    )
    def test_resumable_terminal_flips_to_running(self, tmp_path, status):
        _write_state(tmp_path, {
            "pc": "build",
            "status": status,
            "retry_counts": {"build": 3},
            "node_execution_count": {"build": 10},
            "error": {"code": "SOMETHING"},
        })
        eng = _engine(tmp_path)
        result = eng._check_and_recover()
        assert result == "resumed_failed"
        assert eng.state["status"] == "running"
        # error marker cleared
        assert "error" not in eng.state
        # current-pc budgets reset
        assert eng.state["retry_counts"]["build"] == 0
        assert eng.state["node_execution_count"]["build"] == 0
        # state was persisted
        with open(os.path.join(tmp_path, ".camflow", "state.json")) as f:
            saved = json.load(f)
        assert saved["status"] == "running"

    def test_running_with_stale_heartbeat_and_dead_pid_recovers(self, tmp_path):
        _write_state(tmp_path, {"pc": "build", "status": "running"})
        # Write a stale heartbeat pointing to a dead pid.
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": 4194301,  # outside Linux default pid_max
                "timestamp": "2020-01-01T00:00:00Z",  # very stale
                "pc": "build",
                "iteration": 1,
                "agent_id": None,
                "uptime_seconds": 60,
            },
        )
        eng = _engine(tmp_path)
        result = eng._check_and_recover()
        assert result == "resumed_crash"
        # Status stays 'running' — we don't flip; we just continue.
        assert eng.state["status"] == "running"

    def test_running_with_fresh_heartbeat_no_action(self, tmp_path):
        _write_state(tmp_path, {"pc": "build", "status": "running"})
        # Fresh heartbeat → recovery returns None.
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": os.getpid(),
                "timestamp": _utcnow_iso(),
                "pc": "build",
                "iteration": 1,
                "agent_id": None,
                "uptime_seconds": 5,
            },
        )
        eng = _engine(tmp_path)
        assert eng._check_and_recover() is None

    def test_running_with_no_heartbeat_no_action(self, tmp_path):
        """No heartbeat file means "never ran" — nothing to recover."""
        _write_state(tmp_path, {"pc": "build", "status": "running"})
        eng = _engine(tmp_path)
        assert eng._check_and_recover() is None


# ---- Engine.run idempotent short-circuit ------------------------------


class TestRunIdempotent:
    def test_already_done_short_circuits_without_lock_or_heartbeat(self, tmp_path):
        """Re-running a finished workflow must not acquire the lock.

        This is what makes `camflow run workflow.yaml` safe to type a
        second time after completion.
        """
        _write_state(tmp_path, {"pc": "verify", "status": "done"})
        eng = _engine(tmp_path)

        with patch("camflow.backend.cam.engine.EngineLock") as Lock, \
             patch("camflow.backend.cam.engine.HeartbeatThread") as HB:
            result = eng.run()
            Lock.assert_not_called()
            HB.assert_not_called()
        assert result["status"] == "done"
