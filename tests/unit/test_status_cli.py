"""CLI tests for `camflow status`.

We stub out everything the command touches in .camflow/ and verify
it prints the ALIVE / DEAD / IDLE distinctions and returns the right
exit code in each.
"""

from __future__ import annotations

import argparse
import os

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


def _args(workflow):
    return argparse.Namespace(workflow=workflow, project_dir=None)


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
