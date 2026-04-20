"""Unit tests for the DSL v2 preflight gate.

Preflight runs BEFORE a node body (cmd or agent). On non-zero exit the
body is skipped entirely; on zero exit execution proceeds as normal.
"""

from unittest.mock import MagicMock, patch

from camflow.backend.cam.engine import Engine
from camflow.engine.state_enricher import init_structured_fields


def _engine(tmp_path, node, project_dir=None):
    """Minimal Engine stub that bypasses workflow I/O."""
    eng = Engine.__new__(Engine)
    eng.state = init_structured_fields({"pc": "n", "status": "running"})
    eng.project_dir = str(project_dir or tmp_path)
    eng.state_path = str(tmp_path / ".camflow" / "state.json")
    eng.workflow = {"n": node}
    return eng


class TestPreflightDirect:
    """Test _run_preflight() in isolation — no _run_node dispatch yet."""

    def test_no_preflight_returns_none(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo"})
        assert eng._run_preflight({"do": "shell echo"}) is None

    def test_preflight_pass_returns_none(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo", "preflight": "true"})
        assert eng._run_preflight({"preflight": "true"}) is None

    def test_preflight_fail_returns_result(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo", "preflight": "false"})
        result = eng._run_preflight({"preflight": "false"})
        assert result is not None
        assert result["status"] == "fail"
        assert result["error"]["code"] == "PREFLIGHT_FAIL"
        assert result["error"]["exit_code"] != 0
        assert "preflight failed" in result["summary"]

    def test_preflight_template_substitution(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo"})
        eng.state["flag"] = "yes"
        # Renders to `test yes = yes` → exits 0 → preflight passes
        assert eng._run_preflight({"preflight": "test {{state.flag}} = yes"}) is None

    def test_preflight_timeout_captured(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo"})
        # The implementation hard-limits preflight to 60s; we can't
        # easily trigger that in a unit test. Stub subprocess.run.
        with patch(
            "camflow.backend.cam.engine.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("test", 60),
        ):
            result = eng._run_preflight({"preflight": "sleep 999"})
        assert result["status"] == "fail"
        assert result["error"]["code"] == "PREFLIGHT_TIMEOUT"

    def test_preflight_unexpected_exception(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo"})
        with patch(
            "camflow.backend.cam.engine.subprocess.run",
            side_effect=OSError("disk full"),
        ):
            result = eng._run_preflight({"preflight": "true"})
        assert result["status"] == "fail"
        assert result["error"]["code"] == "PREFLIGHT_ERROR"


class TestPreflightInDispatch:
    """Integration-ish: verify _run_node honors the preflight gate."""

    def test_preflight_fail_skips_shell_body(self, tmp_path):
        eng = _engine(tmp_path, {"do": "shell echo should-not-run", "preflight": "false"})
        eng.config = MagicMock(node_timeout=60)
        with patch("camflow.backend.cam.engine.run_cmd") as run_cmd:
            result, agent_id, signal = eng._run_node(
                "n", {"do": "shell echo should-not-run", "preflight": "false"},
                attempt=1, is_retry=False,
            )
        assert result["status"] == "fail"
        assert result["error"]["code"] == "PREFLIGHT_FAIL"
        assert signal == "preflight_fail"
        assert agent_id is None
        run_cmd.assert_not_called()

    def test_preflight_fail_skips_agent_body(self, tmp_path):
        node = {"do": "agent placeholder", "preflight": "false"}
        eng = _engine(tmp_path, node)
        eng.config = MagicMock(node_timeout=60, max_retries=3, poll_interval=5)
        with patch(
            "camflow.backend.cam.agent_runner.start_agent"
        ) as start_agent:
            result, agent_id, signal = eng._run_node(
                "n", node, attempt=1, is_retry=False,
            )
        assert result["error"]["code"] == "PREFLIGHT_FAIL"
        assert signal == "preflight_fail"
        start_agent.assert_not_called()

    def test_preflight_pass_proceeds_to_shell(self, tmp_path):
        node = {"do": "shell echo ok", "preflight": "true"}
        eng = _engine(tmp_path, node)
        eng.config = MagicMock(node_timeout=60)
        fake_result = {"status": "success", "summary": "ran"}
        with patch(
            "camflow.backend.cam.engine.run_cmd", return_value=fake_result,
        ) as run_cmd:
            result, agent_id, signal = eng._run_node(
                "n", node, attempt=1, is_retry=False,
            )
        run_cmd.assert_called_once()
        assert result is fake_result
