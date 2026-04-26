"""Unit tests for backend.cam.tracer."""

import time

import pytest

from camflow.backend.cam.tracer import (
    EVENT_KINDS,
    STEP_KIND,
    approx_token_count,
    build_event_entry,
    build_trace_entry,
    is_step,
)


def test_all_fields_present():
    ts = time.time()
    e = build_trace_entry(
        step=1,
        node_id="fix",
        node={"do": "agent placeholder"},
        input_state={"pc": "fix"},
        node_result={"status": "success"},
        output_state={"pc": "test"},
        transition={"next_pc": "test", "workflow_status": "running", "reason": "next"},
        ts_start=ts,
        ts_end=ts + 0.5,
    )
    expected_fields = {
        "kind", "step", "ts_start", "ts_end", "duration_ms",
        "node_id", "do", "attempt", "is_retry", "retry_mode",
        "input_state", "node_result", "output_state", "transition",
        "agent_id", "exec_mode", "completion_signal",
        "lesson_added", "event",
        # evaluation fields
        "prompt_tokens", "context_tokens", "task_tokens",
        "tools_available", "tools_used", "context_position",
        "enricher_enabled", "fenced", "methodology", "escalation_level",
    }
    assert set(e.keys()) >= expected_fields


def test_step_entry_kind_is_step():
    """build_trace_entry stamps every entry with kind='step'."""
    e = build_trace_entry(1, "n", {}, {}, {}, {}, {}, 0.0, 0.1)
    assert e["kind"] == STEP_KIND == "step"


def test_duration_ms():
    ts = time.time()
    e = build_trace_entry(1, "n", {}, {}, {}, {}, {}, ts_start=ts, ts_end=ts + 1.5)
    assert e["duration_ms"] == 1500


def test_deep_copies_inputs():
    """Mutating input_state/output_state after build_trace_entry must not affect the entry."""
    input_state = {"pc": "fix", "lessons": ["a"]}
    output_state = {"pc": "test"}
    node_result = {"status": "success", "state_updates": {"x": 1}}

    e = build_trace_entry(
        1, "fix", {"do": "cmd x"}, input_state, node_result, output_state,
        {"next_pc": "test"}, 0.0, 0.1,
    )

    input_state["lessons"].append("b")
    output_state["pc"] = "done"
    node_result["state_updates"]["x"] = 999

    assert e["input_state"]["lessons"] == ["a"]
    assert e["output_state"]["pc"] == "test"
    assert e["node_result"]["state_updates"]["x"] == 1


def test_iso_timestamps():
    e = build_trace_entry(1, "n", {}, {}, {}, {}, {}, 0.0, 1.234)
    # starts with 1970, ends with 'Z', has millisecond precision (3 digits after dot)
    assert e["ts_start"].startswith("1970-01-01T00:00:00")
    assert e["ts_start"].endswith("Z")
    assert "." in e["ts_start"]


def test_evaluation_field_defaults():
    """Evaluation fields default to values that describe current behavior."""
    e = build_trace_entry(1, "n", {}, {}, {}, {}, {}, 0.0, 0.1)
    assert e["prompt_tokens"] is None
    assert e["context_tokens"] is None
    assert e["task_tokens"] is None
    assert e["tools_available"] is None
    assert e["tools_used"] is None
    assert e["context_position"] == "middle"
    assert e["enricher_enabled"] is True
    assert e["fenced"] is True
    assert e["methodology"] == "none"
    assert e["escalation_level"] == 0


def test_evaluation_field_override():
    """All evaluation fields are settable via keyword args."""
    e = build_trace_entry(
        1, "fix", {"do": "agent placeholder"}, {}, {}, {}, {}, 0.0, 0.1,
        prompt_tokens=4500, context_tokens=2100, task_tokens=800,
        tools_available=4, tools_used=2, context_position="first",
        enricher_enabled=False, fenced=False, methodology="rca",
        escalation_level=2,
    )
    assert e["prompt_tokens"] == 4500
    assert e["context_tokens"] == 2100
    assert e["task_tokens"] == 800
    assert e["tools_available"] == 4
    assert e["tools_used"] == 2
    assert e["context_position"] == "first"
    assert e["enricher_enabled"] is False
    assert e["fenced"] is False
    assert e["methodology"] == "rca"
    assert e["escalation_level"] == 2


class TestBuildEventEntry:
    def test_minimal_event(self):
        e = build_event_entry(
            "agent_spawned",
            actor="engine",
            flow_id="flow_001",
            ts=1745233000.0,
            agent_id="camflow-build-a1b2c3",
            role="worker",
            node_id="build",
        )
        assert e["kind"] == "agent_spawned"
        assert e["actor"] == "engine"
        assert e["flow_id"] == "flow_001"
        assert e["agent_id"] == "camflow-build-a1b2c3"
        assert e["role"] == "worker"
        assert e["node_id"] == "build"
        assert e["ts"].startswith("2025-")  # 1745233000 → April 2025

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown event kind"):
            build_event_entry("not_a_real_kind", actor="engine")

    def test_project_level_event_flow_id_none(self):
        """Steward handoff is not bound to a flow."""
        e = build_event_entry(
            "handoff_completed",
            actor="engine",
            from_agent="steward-7c2a",
            to_agent="steward-7c2a-v2",
        )
        assert e["flow_id"] is None
        assert e["from_agent"] == "steward-7c2a"

    def test_ts_defaults_to_now(self):
        before = time.time()
        e = build_event_entry("flow_started", actor="engine", flow_id="f1")
        after = time.time()
        # ts is ISO-formatted; just check the year matches current
        from datetime import datetime, timezone
        parsed_ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()
        assert before - 1 <= parsed_ts <= after + 1

    def test_all_event_kinds_accepted(self):
        for kind in EVENT_KINDS:
            e = build_event_entry(kind, actor="engine", flow_id="f1")
            assert e["kind"] == kind


class TestIsStep:
    def test_kind_step_is_step(self):
        assert is_step({"kind": "step", "step": 1}) is True

    def test_kind_event_is_not_step(self):
        assert is_step({"kind": "agent_spawned"}) is False

    def test_missing_kind_is_step_for_backward_compat(self):
        """Old entries without the kind field are treated as steps."""
        assert is_step({"step": 7, "node_id": "build"}) is True


class TestApproxTokenCount:
    def test_empty_text_is_zero(self):
        assert approx_token_count("") == 0
        assert approx_token_count(None) == 0

    def test_minimum_one_token_for_any_content(self):
        assert approx_token_count("a") == 1
        assert approx_token_count("abc") == 1

    def test_ratio_is_4_chars_per_token(self):
        assert approx_token_count("a" * 8) == 2
        assert approx_token_count("a" * 400) == 100

    def test_deterministic(self):
        text = "hello world, this is a test string"
        assert approx_token_count(text) == approx_token_count(text)
