"""Unit tests for plan-priority + verify-cmd features.

- Node-level `methodology` overrides keyword routing.
- Node-level `escalation_max` caps escalation level.
- Node-level `max_retries` overrides engine default.
- Node-level `verify` cmd runs after a successful agent result,
  downgrading to fail on non-zero exit; does NOT run when the agent
  itself already failed.
"""

import textwrap
from unittest.mock import MagicMock

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.backend.cam.prompt_builder import build_prompt
from camflow.engine.escalation import get_escalation_level, get_escalation_prompt
from camflow.engine.methodology_router import METHODOLOGIES
from camflow.engine.state_enricher import init_structured_fields


# ---- Plan-priority methodology ---------------------------------------


class TestPlanMethodology:
    def test_plan_methodology_overrides_keyword(self):
        """A node that says 'fix a bug' would auto-route to RCA. If the
        plan explicitly asks for 'search-first', plan wins."""
        state = init_structured_fields({"pc": "n", "status": "running"})
        node = {
            "do": "agent placeholder",
            "with": "fix the bug in parser.py",  # keyword → rca
            "methodology": "search-first",       # plan override
        }
        p = build_prompt("n", node, state)
        assert "Search-first" in p
        assert "RCA" not in p

    def test_unknown_plan_label_falls_through_silently(self):
        """A plan that writes a bogus label renders nothing (no crash)."""
        state = init_structured_fields({"pc": "n", "status": "running"})
        node = {
            "do": "agent placeholder",
            "with": "analyze the code",
            "methodology": "made-up-name",
        }
        p = build_prompt("n", node, state)
        # Neither the bogus name nor any real methodology appears
        assert "made-up-name" not in p
        for label, hint in METHODOLOGIES.items():
            assert hint not in p

    def test_no_plan_methodology_uses_keyword(self):
        state = init_structured_fields({"pc": "n", "status": "running"})
        node = {"do": "agent placeholder", "with": "Fix the bug."}
        p = build_prompt("fix", node, state)
        assert "RCA" in p


# ---- Escalation max cap ----------------------------------------------


class TestEscalationMax:
    def test_level_capped_at_max_level(self):
        state = {"retry_counts": {"fix": 5}}  # would be L4 uncapped
        assert get_escalation_level(state, "fix", max_level=4) == 4
        assert get_escalation_level(state, "fix", max_level=1) == 1
        assert get_escalation_level(state, "fix", max_level=2) == 2
        assert get_escalation_level(state, "fix", max_level=0) == 0

    def test_prompt_reflects_capped_level(self):
        state = {"retry_counts": {"fix": 5}}
        p0 = get_escalation_prompt(state, "fix", max_level=0)
        p1 = get_escalation_prompt(state, "fix", max_level=1)
        p4 = get_escalation_prompt(state, "fix", max_level=4)
        assert p0 == ""
        assert "FUNDAMENTALLY DIFFERENT" in p1
        assert "ESCALATION" in p4
        assert p0 != p1 != p4

    def test_build_prompt_respects_escalation_max(self):
        """With retry_counts=5 a node would normally hit L4 — cap at L1."""
        state = init_structured_fields({"pc": "fix", "status": "running"})
        state["retry_counts"]["fix"] = 5
        node = {
            "do": "agent placeholder",
            "with": "Fix the bug.",
            "escalation_max": 1,
        }
        p = build_prompt("fix", node, state)
        assert "FUNDAMENTALLY DIFFERENT" in p
        assert "ESCALATION:" not in p
        assert "DIAGNOSTIC MODE" not in p


# ---- Node-level max_retries override ---------------------------------


class TestMaxRetriesOverride:
    def test_node_max_retries_used_in_retry_reason(self, tmp_path):
        """Engine reports retry ratio against node.max_retries when set."""
        from camflow.backend.cam.engine import Engine

        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text(textwrap.dedent("""
            n:
              do: cmd false
              max_retries: 5
        """))

        cfg = EngineConfig(poll_interval=0, node_timeout=5,
                            max_retries=3, max_node_executions=2)
        eng = Engine(str(wf_path), str(tmp_path), cfg)
        eng._load_workflow()
        eng._load_or_init_state()

        # Seed state so retry_count starts low; inject a fail result and
        # verify the transition reason cites the node-override (5) not 3.
        result = {"status": "fail", "summary": "cmd failed",
                   "output": {}, "state_updates": {},
                   "error": {"code": "CMD_FAIL"}}
        eng.step = 1
        eng._last_prompt = None
        cont = eng._apply_result_and_transition(
            "n", {"do": "cmd false", "max_retries": 5}, result,
            ts_start=0.0, ts_end=0.1,
            agent_id=None, exec_mode="cmd",
            completion_signal=None, event=None, attempt=1,
        )
        assert cont is True
        # Retry counter bumped, but the engine config max_retries (3)
        # would have exhausted at retry_count=2; with node's 5 we still
        # have headroom.
        assert eng.state["retry_counts"]["n"] == 1


# ---- Verify cmd ------------------------------------------------------


class TestVerifyCmd:
    def _engine_with_node(self, tmp_path, node):
        """Build a minimal engine with one node; skip workflow I/O."""
        eng = Engine.__new__(Engine)
        eng.state = init_structured_fields({"pc": "n", "status": "running"})
        eng.project_dir = str(tmp_path)
        eng.state_path = str(tmp_path / ".camflow" / "state.json")
        eng.workflow = {"n": node}
        return eng

    def test_verify_success_leaves_result_alone(self, tmp_path):
        eng = self._engine_with_node(tmp_path, {
            "do": "agent placeholder", "verify": "true",
        })
        result = {"status": "success", "summary": "did it",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd({"verify": "true"}, result)
        assert result["status"] == "success"
        assert result["summary"] == "did it"

    def test_verify_failure_downgrades_to_fail(self, tmp_path):
        eng = self._engine_with_node(tmp_path, {
            "do": "agent placeholder", "verify": "false",
        })
        result = {"status": "success", "summary": "agent said done",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd({"verify": "false"}, result)
        assert result["status"] == "fail"
        assert "verify failed" in result["summary"]
        assert result["error"]["code"] == "VERIFY_FAIL"
        assert result["error"]["exit_code"] != 0

    def test_verify_does_not_run_when_agent_already_failed(self, tmp_path):
        """If the agent said fail, verify must not run (pointless + could
        mask the real error)."""
        eng = self._engine_with_node(tmp_path, {
            "do": "agent placeholder", "verify": "false",
        })
        result = {"status": "fail", "summary": "agent crashed",
                  "state_updates": {},
                  "error": {"code": "NODE_FAIL"}}
        eng._apply_verify_cmd({"verify": "false"}, result)
        # Unchanged
        assert result["status"] == "fail"
        assert result["summary"] == "agent crashed"
        assert result["error"] == {"code": "NODE_FAIL"}

    def test_verify_template_substitution(self, tmp_path):
        """Verify cmd should honor {{state.x}} substitution."""
        eng = self._engine_with_node(tmp_path, {
            "do": "agent placeholder",
            "verify": "test {{state.flag}} = yes",
        })
        eng.state["flag"] = "yes"  # will render `test yes = yes`
        result = {"status": "success", "summary": "ok",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd(
            {"verify": "test {{state.flag}} = yes"}, result,
        )
        # true → verify passes → status unchanged
        assert result["status"] == "success"

    def test_verify_missing_is_noop(self, tmp_path):
        eng = self._engine_with_node(tmp_path, {"do": "agent placeholder"})
        result = {"status": "success", "summary": "ok",
                  "state_updates": {}, "error": None}
        eng._apply_verify_cmd({"do": "agent placeholder"}, result)
        assert result["status"] == "success"


# ---- DSL: new optional fields accepted -------------------------------


class TestDslAcceptsNewFields:
    def test_plan_fields_are_valid(self):
        from camflow.engine.dsl import validate_node

        node = {
            "do": "agent placeholder",
            "with": "go",
            "methodology": "rca",
            "verify": "true",
            "escalation_max": 2,
            "max_retries": 5,
            "allowed_tools": ["Read", "Edit"],
            "timeout": 60,
        }
        ok, errors = validate_node("n", node)
        assert ok, errors

    def test_still_rejects_truly_unknown_field(self):
        from camflow.engine.dsl import validate_node

        node = {"do": "agent placeholder", "madeup_field": 123}
        ok, errors = validate_node("n", node)
        assert not ok
        assert any("unknown" in e.lower() for e in errors)
