"""Integration tests for the plan → execution boundary.

Complements tests/unit/test_plan_priority.py by covering cases the
planner spec explicitly calls out that aren't already exercised:

  * allowed_tools from node config reaches start_agent
  * methodology from node config renders in the prompt
  * escalation cap applies even at high retry counts
"""

from __future__ import annotations

import textwrap

import pytest

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.backend.cam.prompt_builder import build_prompt
from camflow.engine.escalation import get_escalation_prompt
from camflow.engine.state_enricher import init_structured_fields


class TestAllowedToolsPassedToStartAgent:
    def test_engine_passes_allowed_tools_kwarg(self, tmp_path, monkeypatch):
        """When a node declares allowed_tools, the engine forwards it to
        start_agent. Agent spawn is monkeypatched to capture the kwargs."""
        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text(textwrap.dedent("""
            start:
              do: agent placeholder
              with: "do it"
              methodology: rca
              escalation_max: 2
              max_retries: 1
              allowed_tools: [Read, Edit, Bash]
              verify: "true"
        """))

        captured = {}

        def fake_start(node_id, prompt, project_dir, allowed_tools=None):
            captured["node_id"] = node_id
            captured["allowed_tools"] = allowed_tools
            return "agent01"

        def fake_wait(*a, **kw):
            return ("file_appeared", None)

        def fake_finalize(agent_id, signal, project_dir, cleanup=True):
            return {
                "status": "success",
                "summary": "did it",
                "state_updates": {},
                "output": {},
                "error": None,
            }

        from camflow.backend.cam import agent_runner
        monkeypatch.setattr(agent_runner, "start_agent", fake_start)
        monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
        monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)
        monkeypatch.setattr(agent_runner, "kill_existing_camflow_agents",
                             lambda *a, **kw: None)
        monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents",
                             lambda: None)
        monkeypatch.setattr(agent_runner, "_cleanup_agent",
                             lambda aid: None)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
        eng = Engine(str(wf_path), str(tmp_path), cfg)
        eng.run()

        assert captured["node_id"] == "start"
        assert captured["allowed_tools"] == ["Read", "Edit", "Bash"]

    def test_engine_omits_allowed_tools_when_absent(self, tmp_path, monkeypatch):
        """Nodes without `allowed_tools` still work — engine passes None."""
        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text(textwrap.dedent("""
            start:
              do: agent placeholder
              with: "do it"
              methodology: rca
              escalation_max: 2
              max_retries: 1
              verify: "true"
        """))

        captured = {}

        def fake_start(node_id, prompt, project_dir, allowed_tools=None):
            captured["allowed_tools"] = allowed_tools
            return "agent01"

        def fake_wait(*a, **kw):
            return ("file_appeared", None)

        def fake_finalize(*a, **kw):
            return {"status": "success", "summary": "ok",
                    "state_updates": {}, "output": {}, "error": None}

        from camflow.backend.cam import agent_runner
        monkeypatch.setattr(agent_runner, "start_agent", fake_start)
        monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
        monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)
        monkeypatch.setattr(agent_runner, "kill_existing_camflow_agents",
                             lambda *a, **kw: None)
        monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents",
                             lambda: None)
        monkeypatch.setattr(agent_runner, "_cleanup_agent",
                             lambda aid: None)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
        eng = Engine(str(wf_path), str(tmp_path), cfg)
        eng.run()

        assert captured["allowed_tools"] is None


class TestPlanMethodologyInjected:
    def test_plan_label_shows_in_prompt(self):
        state = init_structured_fields({"pc": "n", "status": "running"})
        node = {
            "do": "agent placeholder",
            "with": "analyze the bug",  # keyword → rca
            "methodology": "working-backwards",  # plan override
        }
        p = build_prompt("n", node, state)
        assert "Working-backwards" in p
        # Keyword-derived methodology not present
        assert "RCA" not in p


class TestEscalationCapRespected:
    def test_cap_prevents_L4_at_high_retries(self):
        state = {"retry_counts": {"fix": 10}}  # uncapped → L4
        p = get_escalation_prompt(state, "fix", max_level=2)
        # L2 prompt wins; L4 prompt must not appear
        assert "DEEP DIVE" in p
        assert "ESCALATION:" not in p
        assert "DIAGNOSTIC MODE" not in p

    def test_cap_of_zero_produces_empty(self):
        state = {"retry_counts": {"fix": 10}}
        p = get_escalation_prompt(state, "fix", max_level=0)
        assert p == ""


class TestVerifyTemplateSubstitution:
    """Covers the 'verify cmd template vars resolved from state' spec item."""

    def test_verify_uses_resolved_state_values(self, tmp_path):
        """verify='test {{state.flag}}' resolves against state."""
        eng = Engine.__new__(Engine)
        eng.state = init_structured_fields({"pc": "n", "status": "running"})
        eng.project_dir = str(tmp_path)
        eng.state_path = str(tmp_path / ".camflow" / "state.json")
        eng.workflow = {"n": {"do": "agent placeholder", "verify": "test {{state.expected}} = ok"}}

        # With state.expected == "ok", `test ok = ok` exits 0 → verify passes
        eng.state["expected"] = "ok"
        result = {"status": "success", "summary": "agent done",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd(
            {"verify": "test {{state.expected}} = ok"}, result,
        )
        assert result["status"] == "success"

        # Now flip the state so the resolved cmd fails
        eng.state["expected"] = "nope"
        result = {"status": "success", "summary": "agent done",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd(
            {"verify": "test {{state.expected}} = ok"}, result,
        )
        assert result["status"] == "fail"
        assert result["error"]["code"] == "VERIFY_FAIL"
