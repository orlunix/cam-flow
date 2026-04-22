"""CLI tests for `camflow status`.

We stub out everything the command touches in .camflow/ and verify
it prints the ALIVE / DEAD / IDLE distinctions and returns the right
exit code in each.
"""

from __future__ import annotations

import argparse
import os
import time

from camflow.backend.persistence import save_state_atomic
from camflow.cli_entry.status import status_command
from camflow.engine.monitor import (
    _utcnow_iso,
    heartbeat_path,
    write_heartbeat,
)


def _wf(tmp_path):
    p = tmp_path / "workflow.yaml"
    p.write_text(
        "build:\n  do: shell true\n  next: verify\n"
        "verify:\n  do: shell true\n"
    )
    return str(p)


def _args(workflow=None, project_dir=None):
    return argparse.Namespace(workflow=workflow, project_dir=project_dir)


def _state(tmp_path, **overrides):
    base = {"pc": "build", "status": "running", "completed": []}
    base.update(overrides)
    save_state_atomic(str(tmp_path / ".camflow" / "state.json"), base)


class TestStatusCLI:
    def test_idle_when_no_state(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "State:    none" in out
        assert rc == 2

    def test_alive_engine(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path)
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": os.getpid(),
                "timestamp": _utcnow_iso(),
                "pc": "build",
                "iteration": 1,
                "agent_id": "abc123",
                "uptime_seconds": 42,
            },
        )
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "Engine:   ALIVE" in out
        assert f"pid {os.getpid()}" in out
        assert "abc123" in out
        assert rc == 0

    def test_dead_engine_is_resumable(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path)
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": 4194301,  # guaranteed-dead
                "timestamp": "2020-01-01T00:00:00Z",
                "pc": "build",
                "iteration": 2,
                "agent_id": "5130c656",
                "uptime_seconds": 600,
            },
        )
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "Engine:   DEAD" in out
        assert "was in progress" in out
        assert "Recovery:" in out
        assert rc == 1

    def test_failed_terminal_state_prompts_resume(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path, status="failed")
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "Engine:   IDLE" in out
        assert "Recovery:" in out
        assert "prev status: failed" in out
        assert rc == 1

    def test_progress_bars_show_done_current_pending(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path, pc="verify", completed=[{"node": "build"}])
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": os.getpid(),
                "timestamp": _utcnow_iso(),
                "pc": "verify",
                "iteration": 2,
                "agent_id": None,
                "uptime_seconds": 120,
                "workflow_path": wf,
            },
        )
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "[done] build" in out
        assert "[>>>>] verify" in out
        assert "Progress: 1/2 nodes completed" in out
        assert "iteration 2" in out
        assert rc == 0

    def test_dead_engine_shows_crash_marker(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path, pc="build", completed=[])
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": 4194301,
                "timestamp": "2020-01-01T00:00:00Z",
                "pc": "build",
            },
        )
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        assert "[XXXX] build" in out
        assert "Engine:   DEAD" in out
        assert rc == 1

    def test_workflow_arg_optional_reads_heartbeat(self, tmp_path, capsys):
        """No workflow arg → discover via heartbeat's workflow_path."""
        wf = _wf(tmp_path)
        _state(tmp_path)
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": os.getpid(),
                "timestamp": _utcnow_iso(),
                "pc": "build",
                "workflow_path": wf,
                "uptime_seconds": 5,
            },
        )
        rc = status_command(_args(workflow=None, project_dir=str(tmp_path)))
        out = capsys.readouterr().out
        assert wf in out
        assert "Engine:   ALIVE" in out
        assert rc == 0

    def test_agent_duration_shown_when_started_at_known(self, tmp_path, capsys):
        wf = _wf(tmp_path)
        _state(tmp_path, current_agent_id="abc123", current_node_started_at=time.time() - 125)
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": os.getpid(),
                "timestamp": _utcnow_iso(),
                "pc": "build",
                "agent_id": "abc123",
                "agent_started_at": time.time() - 125,
                "uptime_seconds": 150,
            },
        )
        rc = status_command(_args(wf))
        out = capsys.readouterr().out
        # Duration is formatted as "Xm Ys"; we just check the agent line
        # has a running duration.
        assert "abc123 (running" in out
        assert "m " in out  # at least one minute of duration
        assert rc == 0
