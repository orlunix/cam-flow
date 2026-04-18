"""Unit tests for backend.cam.orphan_handler.decide_orphan_action.

All tests mock _get_agent_status; none spawn real agents.
"""

import os

import pytest

from camflow.backend.cam import orphan_handler


class TestDecideOrphanAction:
    def test_no_current_agent(self, tmp_path):
        state = {"pc": "fix"}
        action, aid = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_NO_ORPHAN
        assert aid is None

    def test_agent_gone_no_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status", lambda _id: None)
        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, aid = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_TREAT_AS_CRASH
        assert aid == "abc123"

    def test_agent_gone_but_result_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status", lambda _id: None)
        os.makedirs(tmp_path / ".camflow")
        (tmp_path / ".camflow" / "node-result.json").write_text('{"status":"success","summary":"x"}')

        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, _aid = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_ADOPT_RESULT

    def test_agent_still_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status",
                            lambda _id: {"status": "running", "state": "busy"})
        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, _ = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_WAIT

    def test_agent_completed_with_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status",
                            lambda _id: {"status": "completed", "state": "idle"})
        os.makedirs(tmp_path / ".camflow")
        (tmp_path / ".camflow" / "node-result.json").write_text('{"status":"success","summary":"x"}')

        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, _ = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_ADOPT_RESULT

    def test_agent_completed_no_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status",
                            lambda _id: {"status": "completed", "state": "idle"})
        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, _ = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_TREAT_AS_CRASH

    def test_agent_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orphan_handler, "_get_agent_status",
                            lambda _id: {"status": "failed", "state": "idle"})
        state = {"pc": "fix", "current_agent_id": "abc123"}
        action, _ = orphan_handler.decide_orphan_action(state, str(tmp_path))
        assert action == orphan_handler.ACTION_TREAT_AS_CRASH


class TestHandleOrphan:
    def test_no_orphan_raises(self, tmp_path):
        with pytest.raises(ValueError):
            orphan_handler.handle_orphan(
                orphan_handler.ACTION_NO_ORPHAN, None, str(tmp_path), 30, 1,
            )

    def test_treat_as_crash_synthesizes_fail(self, tmp_path, monkeypatch):
        # Don't actually call camc rm
        from camflow.backend.cam import agent_runner
        monkeypatch.setattr(agent_runner, "_cleanup_agent", lambda _id: None)

        result, signal = orphan_handler.handle_orphan(
            orphan_handler.ACTION_TREAT_AS_CRASH, "abc123", str(tmp_path), 30, 1,
        )
        assert result["status"] == "fail"
        assert result["error"]["code"] == "AGENT_CRASH"
        assert signal == "adopted_crash"

    def test_adopt_result_reads_file(self, tmp_path, monkeypatch):
        # Don't actually call camc rm
        from camflow.backend.cam import agent_runner
        monkeypatch.setattr(agent_runner, "_cleanup_agent", lambda _id: None)

        os.makedirs(tmp_path / ".camflow")
        (tmp_path / ".camflow" / "node-result.json").write_text(
            '{"status":"success","summary":"done by orphan","state_updates":{}}'
        )

        result, signal = orphan_handler.handle_orphan(
            orphan_handler.ACTION_ADOPT_RESULT, "abc123", str(tmp_path), 30, 1,
        )
        assert result["status"] == "success"
        assert "orphan" in result["summary"]
        assert signal == "adopted_result"
