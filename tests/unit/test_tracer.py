"""Unit tests for backend.cam.tracer.build_trace_entry."""

import time

from camflow.backend.cam.tracer import build_trace_entry


def test_all_fields_present():
    ts = time.time()
    e = build_trace_entry(
        step=1,
        node_id="fix",
        node={"do": "agent claude"},
        input_state={"pc": "fix"},
        node_result={"status": "success"},
        output_state={"pc": "test"},
        transition={"next_pc": "test", "workflow_status": "running", "reason": "next"},
        ts_start=ts,
        ts_end=ts + 0.5,
    )
    expected_fields = {
        "step", "ts_start", "ts_end", "duration_ms",
        "node_id", "do", "attempt", "is_retry", "retry_mode",
        "input_state", "node_result", "output_state", "transition",
        "agent_id", "exec_mode", "completion_signal",
        "lesson_added", "event",
    }
    assert set(e.keys()) >= expected_fields


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
