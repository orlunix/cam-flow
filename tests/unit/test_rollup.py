"""Unit tests for camflow.evolution.rollup."""

import json

from camflow.evolution.rollup import (
    print_report,
    rollup_all,
    rollup_trace,
)


def _trace_line(step, node_id, status, duration_ms=100, exec_mode="cmd",
                methodology="none", retry_mode=None, escalation_level=0,
                prompt_tokens=None, workflow_status=None):
    return json.dumps({
        "step": step,
        "ts_start": "1970-01-01T00:00:00.000Z",
        "ts_end": "1970-01-01T00:00:00.100Z",
        "duration_ms": duration_ms,
        "node_id": node_id,
        "do": "cmd x" if exec_mode == "cmd" else "agent claude",
        "attempt": 1,
        "is_retry": False,
        "retry_mode": retry_mode,
        "input_state": {},
        "node_result": {"status": status, "summary": "", "state_updates": {},
                        "output": {}, "error": None},
        "output_state": {},
        "transition": {"next_pc": None, "workflow_status": workflow_status, "reason": ""},
        "agent_id": None if exec_mode == "cmd" else "agent01",
        "exec_mode": exec_mode,
        "completion_signal": None if exec_mode == "cmd" else "file_appeared",
        "lesson_added": None,
        "event": None,
        "prompt_tokens": prompt_tokens,
        "context_tokens": None,
        "task_tokens": None,
        "tools_available": None,
        "tools_used": None,
        "context_position": "first",
        "enricher_enabled": True,
        "fenced": True,
        "methodology": methodology,
        "escalation_level": escalation_level,
    })


def _write_trace(tmp_path, lines, sub="run1"):
    d = tmp_path / sub / ".camflow"
    d.mkdir(parents=True)
    p = d / "trace.log"
    p.write_text("\n".join(lines) + "\n")
    return p


class TestRollupTrace:
    def test_empty_trace(self, tmp_path):
        p = tmp_path / "trace.log"
        p.write_text("")
        summary = rollup_trace(str(p))
        assert summary["steps"] == 0
        assert summary["nodes"] == {}

    def test_missing_trace(self, tmp_path):
        summary = rollup_trace(str(tmp_path / "nope.log"))
        assert summary["steps"] == 0

    def test_single_success(self, tmp_path):
        p = _write_trace(tmp_path, [
            _trace_line(1, "fix", "success", duration_ms=100),
        ])
        summary = rollup_trace(str(p))
        assert summary["steps"] == 1
        assert summary["nodes"]["fix"]["runs"] == 1
        assert summary["nodes"]["fix"]["success_rate"] == 1.0
        assert summary["overall"]["avg_duration_ms"] == 100

    def test_multiple_nodes(self, tmp_path):
        lines = [
            _trace_line(1, "fix", "success", duration_ms=500),
            _trace_line(2, "test", "fail", duration_ms=80),
            _trace_line(3, "fix", "success", duration_ms=300),
            _trace_line(4, "test", "success", duration_ms=90),
        ]
        p = _write_trace(tmp_path, lines)
        summary = rollup_trace(str(p))

        assert summary["steps"] == 4
        assert summary["nodes"]["fix"]["runs"] == 2
        assert summary["nodes"]["fix"]["successes"] == 2
        assert summary["nodes"]["fix"]["success_rate"] == 1.0
        assert summary["nodes"]["test"]["runs"] == 2
        assert summary["nodes"]["test"]["successes"] == 1
        assert summary["nodes"]["test"]["success_rate"] == 0.5

    def test_methodology_aggregation(self, tmp_path):
        lines = [
            _trace_line(1, "fix", "success", methodology="rca", exec_mode="camc"),
            _trace_line(2, "fix", "fail",    methodology="rca", exec_mode="camc"),
            _trace_line(3, "done", "success", methodology="none", exec_mode="cmd"),
        ]
        p = _write_trace(tmp_path, lines)
        summary = rollup_trace(str(p))
        assert summary["methodologies"]["rca"]["runs"] == 2
        assert summary["methodologies"]["rca"]["successes"] == 1
        assert summary["methodologies"]["none"]["runs"] == 1

    def test_final_status_from_last_entry(self, tmp_path):
        lines = [
            _trace_line(1, "start", "success"),
            _trace_line(2, "done", "success", workflow_status="done"),
        ]
        p = _write_trace(tmp_path, lines)
        summary = rollup_trace(str(p))
        assert summary["final_status"] == "done"

    def test_prompt_tokens_averaged(self, tmp_path):
        lines = [
            _trace_line(1, "fix", "success", exec_mode="camc", prompt_tokens=2000),
            _trace_line(2, "fix", "success", exec_mode="camc", prompt_tokens=4000),
            # cmd node has no tokens; must not contaminate the avg
            _trace_line(3, "test", "success"),
        ]
        p = _write_trace(tmp_path, lines)
        summary = rollup_trace(str(p))
        assert summary["nodes"]["fix"]["avg_prompt_tokens"] == 3000
        assert summary["nodes"]["test"]["avg_prompt_tokens"] is None

    def test_malformed_trailing_line_skipped(self, tmp_path):
        p = tmp_path / "trace.log"
        p.write_text(
            _trace_line(1, "fix", "success") + "\n"
            + "this is not json\n"
            + _trace_line(2, "test", "fail") + "\n"
        )
        summary = rollup_trace(str(p))
        assert summary["steps"] == 2  # malformed line skipped

    def test_retry_mode_and_escalation_counted(self, tmp_path):
        lines = [
            _trace_line(1, "fix", "fail", retry_mode="task", escalation_level=1),
            _trace_line(2, "fix", "fail", retry_mode="task", escalation_level=2),
            _trace_line(3, "fix", "success", escalation_level=2),
        ]
        p = _write_trace(tmp_path, lines)
        summary = rollup_trace(str(p))
        overall = summary["overall"]
        assert overall["retry_modes"]["task"] == 2
        assert overall["escalation_levels"][1] == 1
        assert overall["escalation_levels"][2] == 2


class TestRollupAll:
    def test_empty_dir(self, tmp_path):
        summary = rollup_all(str(tmp_path))
        assert summary["trace_count"] == 0
        assert summary["steps"] == 0

    def test_single_subproject_trace(self, tmp_path):
        _write_trace(tmp_path, [
            _trace_line(1, "fix", "success"),
            _trace_line(2, "test", "success"),
        ], sub="proj_a")
        summary = rollup_all(str(tmp_path))
        assert summary["trace_count"] == 1
        assert summary["steps"] == 2

    def test_multiple_runs_aggregated(self, tmp_path):
        _write_trace(tmp_path, [
            _trace_line(1, "fix", "success"),
            _trace_line(2, "fix", "fail"),
        ], sub="run1")
        _write_trace(tmp_path, [
            _trace_line(1, "fix", "success"),
            _trace_line(2, "fix", "success"),
        ], sub="run2")
        summary = rollup_all(str(tmp_path))
        assert summary["trace_count"] == 2
        assert summary["steps"] == 4
        # 3 of 4 fix runs succeeded
        assert summary["nodes"]["fix"]["runs"] == 4
        assert summary["nodes"]["fix"]["successes"] == 3

    def test_accepts_file_path(self, tmp_path):
        p = _write_trace(tmp_path, [
            _trace_line(1, "fix", "success"),
        ])
        summary = rollup_all(str(p))
        assert summary["trace_count"] == 1
        assert summary["steps"] == 1


class TestPrintReport:
    def test_emits_expected_keys(self, tmp_path, capsys):
        _write_trace(tmp_path, [
            _trace_line(1, "fix", "success", exec_mode="camc",
                        methodology="rca", prompt_tokens=1200),
            _trace_line(2, "test", "fail"),
        ])
        summary = rollup_all(str(tmp_path))
        print_report(summary)
        out = capsys.readouterr().out
        assert "cam-flow trace rollup" in out
        assert "fix" in out
        assert "test" in out
        assert "rca" in out

    def test_handles_empty_summary(self, capsys):
        summary = {"source": "nowhere", "trace_count": 0, "steps": 0,
                   "nodes": {}, "methodologies": {}, "overall": {
                       "runs": 0, "successes": 0, "fails": 0,
                       "avg_duration_ms": None, "avg_prompt_tokens": None,
                       "methodologies": {}, "exec_modes": {},
                       "retry_modes": {}, "escalation_levels": {},
                       "success_rate": 0.0,
                   }}
        print_report(summary)  # must not raise
        out = capsys.readouterr().out
        assert "cam-flow trace rollup" in out
