"""Unit tests for the Steward integration points in Engine.

These exercise the engine helper methods (``_ensure_steward``,
``_emit_steward_*``) in isolation, without running the full
``Engine.run()`` loop. The full end-to-end test belongs in the
integration suite (which spawns a real camc agent).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.steward.spawn import spawn_steward


def _make_engine(tmp_path: Path, *, no_steward: bool = False) -> Engine:
    """Build an Engine with a minimal workflow file. We don't run it;
    we just need the instance fields populated."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(
        "build:\n"
        "  do: cmd echo ok\n"
    )
    cfg = EngineConfig(reset=True, no_steward=no_steward)
    eng = Engine(str(wf), str(tmp_path), cfg)
    # _emit_steward_* read self.state and self.step, so seed them.
    eng.state = {"flow_id": "flow_test01", "pc": "build", "status": "running"}
    eng.step = 0
    return eng


# ---- EngineConfig.no_steward ------------------------------------------


class TestConfigField:
    def test_default_false(self):
        cfg = EngineConfig()
        assert cfg.no_steward is False

    def test_explicit_true(self):
        cfg = EngineConfig(no_steward=True)
        assert cfg.no_steward is True


# ---- _ensure_steward --------------------------------------------------


class TestEnsureSteward:
    def test_no_steward_skips_spawn(self, tmp_path):
        eng = _make_engine(tmp_path, no_steward=True)
        with patch("camflow.backend.cam.engine.spawn_steward") as m_spawn:
            with patch(
                "camflow.backend.cam.engine.is_steward_alive",
                return_value=False,
            ):
                eng._ensure_steward()
        m_spawn.assert_not_called()

    def test_alive_steward_does_not_respawn(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch(
            "camflow.backend.cam.engine.is_steward_alive",
            return_value=True,
        ):
            with patch(
                "camflow.backend.cam.engine.spawn_steward",
            ) as m_spawn:
                eng._ensure_steward()
        m_spawn.assert_not_called()

    def test_dead_steward_triggers_spawn(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch(
            "camflow.backend.cam.engine.is_steward_alive",
            return_value=False,
        ):
            with patch(
                "camflow.backend.cam.engine.spawn_steward",
            ) as m_spawn:
                eng._ensure_steward()
        assert m_spawn.called
        kwargs = m_spawn.call_args.kwargs
        assert kwargs["workflow_path"] == str(tmp_path / "workflow.yaml")
        assert "spawned_by" in kwargs

    def test_spawn_failure_swallowed_and_logged(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch(
            "camflow.backend.cam.engine.is_steward_alive",
            return_value=False,
        ), patch(
            "camflow.backend.cam.engine.spawn_steward",
            side_effect=RuntimeError("camc gone"),
        ):
            # Must NOT raise — engine continues without Steward.
            eng._ensure_steward()
        # Engine log received the failure.
        log_path = Path(tmp_path) / ".camflow" / "engine.log"
        assert log_path.exists()
        assert "steward spawn/reattach failed" in log_path.read_text()


# ---- _emit_steward_* methods ------------------------------------------


class TestEmitMethods:
    def test_no_steward_skips_all_emits(self, tmp_path):
        eng = _make_engine(tmp_path, no_steward=True)
        with patch("camflow.backend.cam.engine.emit_node_started") as m1, \
             patch("camflow.backend.cam.engine.emit_node_done") as m2, \
             patch("camflow.backend.cam.engine.emit_node_failed") as m3, \
             patch("camflow.backend.cam.engine.emit_flow_started") as m4, \
             patch("camflow.backend.cam.engine.emit_flow_terminal") as m5:
            eng._emit_steward_node_started("build", attempt=1)
            eng._emit_steward_node_finished(
                "build", {"status": "success", "summary": "ok"}, agent_id=None,
            )
            eng._emit_steward_flow_started()
            eng._emit_steward_flow_terminal()
        for m in (m1, m2, m3, m4, m5):
            m.assert_not_called()

    def test_node_started_calls_emitter(self, tmp_path):
        eng = _make_engine(tmp_path)
        eng.step = 5
        with patch("camflow.backend.cam.engine.emit_node_started") as m:
            eng._emit_steward_node_started("build", attempt=2)
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        assert kwargs["flow_id"] == "flow_test01"
        assert kwargs["step"] == 5
        assert kwargs["node"] == "build"
        assert kwargs["attempt"] == 2

    def test_node_finished_routes_success_to_done(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch("camflow.backend.cam.engine.emit_node_done") as m_done, \
             patch("camflow.backend.cam.engine.emit_node_failed") as m_fail:
            eng._emit_steward_node_finished(
                "build",
                {"status": "success", "summary": "compiled"},
                agent_id="cafe",
            )
        m_done.assert_called_once()
        m_fail.assert_not_called()
        kwargs = m_done.call_args.kwargs
        assert kwargs["summary"] == "compiled"
        assert kwargs["agent_id"] == "cafe"

    def test_node_finished_routes_failure_to_failed(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch("camflow.backend.cam.engine.emit_node_done") as m_done, \
             patch("camflow.backend.cam.engine.emit_node_failed") as m_fail:
            eng._emit_steward_node_finished(
                "build",
                {
                    "status": "fail",
                    "summary": "syntax error",
                    "error": {"code": "NODE_FAIL"},
                },
                agent_id="cafe",
            )
        m_fail.assert_called_once()
        m_done.assert_not_called()
        kwargs = m_fail.call_args.kwargs
        assert kwargs["error"]["code"] == "NODE_FAIL"

    def test_flow_started_carries_workflow_path(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch("camflow.backend.cam.engine.emit_flow_started") as m:
            eng._emit_steward_flow_started()
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        assert kwargs["workflow_path"].endswith("workflow.yaml")
        assert kwargs["flow_id"] == "flow_test01"

    def test_flow_terminal_includes_pc_and_status(self, tmp_path):
        eng = _make_engine(tmp_path)
        eng.state["status"] = "done"
        eng.state["pc"] = "deploy"
        with patch("camflow.backend.cam.engine.emit_flow_terminal") as m:
            eng._emit_steward_flow_terminal()
        m.assert_called_once()
        final = m.call_args.kwargs["final"]
        assert final["status"] == "done"
        assert final["pc"] == "deploy"

    def test_emit_failure_swallowed_and_logged(self, tmp_path):
        eng = _make_engine(tmp_path)
        with patch(
            "camflow.backend.cam.engine.emit_node_started",
            side_effect=RuntimeError("camc dead"),
        ):
            # Must NOT raise — engine continues.
            eng._emit_steward_node_started("build", attempt=1)
        log_path = Path(tmp_path) / ".camflow" / "engine.log"
        assert "emit_node_started failed" in log_path.read_text()


# ---- flow_id is generated on fresh state ------------------------------


class TestFlowIdInState:
    def test_init_state_seeds_flow_id(self, tmp_path):
        from camflow.backend.cam.engine import _init_runtime_state
        from camflow.engine.state import init_state

        s = _init_runtime_state(init_state("start"))
        assert "flow_id" in s
        assert s["flow_id"].startswith("flow_")

    def test_init_state_preserves_existing_flow_id(self, tmp_path):
        """Resume preserves flow_id (state is loaded, not re-init'd)."""
        from camflow.backend.cam.engine import _init_runtime_state

        s = _init_runtime_state({"flow_id": "flow_preserved"})
        assert s["flow_id"] == "flow_preserved"
