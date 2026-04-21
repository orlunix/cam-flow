"""Tests for auto-brainstorm on repeated node failure.

The brainstorm feature gives a looping node ONE rescue attempt:
when a node hits max_node_executions the engine spawns a small
brainstorm agent that looks at the failure pattern, returns a
``new_strategy`` via ``state_updates``, and the engine resets the
node's exec_count so the next attempt of the same node runs with
the strategy hint rendered into its context fence. A SECOND hit
on the same node escalates to a real failed-status exit.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from camflow.backend.cam.brainstorm import (
    build_brainstorm_prompt,
    collect_failure_summaries,
)
from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.backend.cam.prompt_builder import build_prompt
from camflow.backend.persistence import append_trace_atomic
from camflow.engine.state_enricher import init_structured_fields


# ---- pure helpers --------------------------------------------------------


class TestCollectFailureSummaries:
    def test_reads_only_matching_node_fails(self, tmp_path):
        trace = tmp_path / "trace.log"
        for entry in [
            {"step": 1, "node_id": "a", "attempt": 1,
             "node_result": {"status": "fail", "summary": "a-err-1",
                              "error": {"code": "E1"}}},
            {"step": 2, "node_id": "b", "attempt": 1,
             "node_result": {"status": "fail", "summary": "b-err"}},
            {"step": 3, "node_id": "a", "attempt": 2,
             "node_result": {"status": "success", "summary": "a-ok"}},
            {"step": 4, "node_id": "a", "attempt": 3,
             "node_result": {"status": "fail", "summary": "a-err-2",
                              "error": {"code": "E2"}}},
        ]:
            append_trace_atomic(str(trace), entry)

        fs = collect_failure_summaries(str(trace), "a")
        assert [f["summary"] for f in fs] == ["a-err-1", "a-err-2"]
        assert [f["error"] for f in fs] == ["E1", "E2"]

    def test_applies_limit(self, tmp_path):
        trace = tmp_path / "trace.log"
        for i in range(20):
            append_trace_atomic(str(trace), {
                "step": i, "node_id": "x", "attempt": i,
                "node_result": {"status": "fail", "summary": f"fail-{i}"},
            })
        fs = collect_failure_summaries(str(trace), "x", limit=3)
        assert [f["summary"] for f in fs] == ["fail-17", "fail-18", "fail-19"]

    def test_handles_missing_file(self, tmp_path):
        fs = collect_failure_summaries(str(tmp_path / "nope.log"), "x")
        assert fs == []


class TestBuildBrainstormPrompt:
    def test_includes_all_failure_summaries(self):
        failures = [
            {"step": 1, "attempt": 1, "summary": "timeout", "error": "T1"},
            {"step": 2, "attempt": 2, "summary": "verify failed", "error": None},
            {"step": 3, "attempt": 3, "summary": "exit 1", "error": "CMD"},
        ]
        node = {"with": "Build the thing"}
        p = build_brainstorm_prompt("build_bpu", node, failures, exec_count=10)
        # All three summaries appear
        assert "timeout" in p
        assert "verify failed" in p
        assert "exit 1" in p
        # Exec count is visible to the brainstormer
        assert "10" in p
        # The task body snippet is visible so the agent knows what was attempted
        assert "Build the thing" in p
        # Contract keywords present
        assert "new_strategy" in p
        assert "node-result.json" in p

    def test_handles_empty_failure_list(self):
        p = build_brainstorm_prompt("n", {}, [], exec_count=10)
        assert "no failure summaries" in p.lower()

    def test_truncates_long_task_body(self):
        huge = "x" * 2000
        p = build_brainstorm_prompt("n", {"with": huge}, [], exec_count=10)
        # 600-char preview + ellipsis
        assert "xxxx" in p
        assert "…" in p
        assert len(p) < 3000


# ---- prompt_builder integration -----------------------------------------


class TestNewStrategyRendersInContext:
    def test_new_strategy_visible_in_next_prompt(self):
        state = init_structured_fields({"pc": "n", "status": "running"})
        state["new_strategy"] = "Switch to a 256-entry 2-bit BHT and bypass IFBN."
        node = {"do": "agent placeholder", "with": "continue the work"}
        p = build_prompt("n", node, state)
        assert "NEW STRATEGY" in p
        assert "256-entry 2-bit BHT" in p
        # Must be strong enough that the agent doesn't silently ignore it
        assert "Do NOT retry the prior approach" in p

    def test_blank_new_strategy_is_skipped(self):
        state = init_structured_fields({"pc": "n", "status": "running"})
        state["new_strategy"] = ""
        node = {"do": "agent placeholder", "with": "continue the work"}
        p = build_prompt("n", node, state)
        assert "NEW STRATEGY" not in p


# ---- engine loop-detection integration ----------------------------------


def _minimal_workflow(tmp_path):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: agent placeholder
          with: "do the thing"
          max_retries: 1
    """))
    return wf


def _seed_state(tmp_path, pc, exec_count, extra=None):
    """Write a pre-cooked state.json so the test can fast-forward past
    a long retry loop and land exactly at the max_node_executions edge."""
    state_dir = tmp_path / ".camflow"
    state_dir.mkdir(exist_ok=True)
    state = init_structured_fields({"pc": pc, "status": "running"})
    state["node_execution_count"] = {pc: exec_count}
    if extra:
        state.update(extra)
    (state_dir / "state.json").write_text(json.dumps(state))
    return state


def _install_agent_fakes(monkeypatch, start_fn=None, wait_fn=None,
                          finalize_fn=None):
    from camflow.backend.cam import agent_runner
    monkeypatch.setattr(agent_runner, "start_agent",
                        start_fn or (lambda node_id, prompt, project_dir, allowed_tools=None: "a1"))
    monkeypatch.setattr(agent_runner, "_wait_for_result",
                        wait_fn or (lambda *a, **kw: ("file_appeared", None)))
    monkeypatch.setattr(agent_runner, "finalize_agent",
                        finalize_fn or (lambda *a, **kw: {
                            "status": "fail",
                            "summary": "agent said no",
                            "state_updates": {}, "output": {}, "error": None,
                        }))
    monkeypatch.setattr(agent_runner, "kill_existing_camflow_agents",
                        lambda *a, **kw: None)
    monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents",
                        lambda: None)
    monkeypatch.setattr(agent_runner, "_cleanup_agent", lambda aid: None)


class TestEngineBrainstormRescue:
    def test_brainstorm_triggers_on_max_executions(self, tmp_path, monkeypatch):
        """First time a node hits max_node_executions, engine calls
        run_agent(brainstorm-*) and does NOT set status=failed."""
        wf = _minimal_workflow(tmp_path)
        _seed_state(tmp_path, pc="start", exec_count=3)

        brainstorm_calls = []

        def fake_run_agent(node_id, prompt, project_dir, timeout=600, poll_interval=5):
            brainstorm_calls.append({
                "node_id": node_id, "prompt": prompt,
            })
            return (
                {"status": "success", "summary": "brainstormed",
                 "state_updates": {"new_strategy": "try Y instead of X"},
                 "error": None},
                "brainstorm_agent_id",
                "file_appeared",
            )

        # Patch the module-level run_agent seen by engine.py
        from camflow.backend.cam import engine as engine_mod
        monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
        _install_agent_fakes(monkeypatch)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1,
                           max_node_executions=3)
        eng = Engine(str(wf), str(tmp_path), cfg)

        # Run ONE _execute_step call directly: hitting the max triggers
        # brainstorm and returns True (continue), WITHOUT marking failed.
        eng._load_workflow()
        eng._load_or_init_state()
        cont = eng._execute_step()

        assert cont is True
        # Brainstorm agent was invoked with the right node_id
        assert brainstorm_calls
        assert brainstorm_calls[0]["node_id"] == "brainstorm-start"
        # State recorded the rescue
        assert eng.state.get("brainstorm_done_for") == ["start"]
        assert eng.state.get("new_strategy") == "try Y instead of X"
        # exec_count for the node was reset
        assert eng.state["node_execution_count"].get("start", 0) == 0
        # Status stays running
        assert eng.state.get("status") == "running"

    def test_second_max_executions_fails_after_brainstorm(self, tmp_path, monkeypatch):
        """If a node already exhausted its brainstorm rescue and hits max
        again, status must become failed."""
        wf = _minimal_workflow(tmp_path)
        _seed_state(tmp_path, pc="start", exec_count=3,
                    extra={"brainstorm_done_for": ["start"]})

        # Brainstorm should NOT be called the second time — patch it to
        # raise so any accidental call fails the test.
        from camflow.backend.cam import engine as engine_mod
        monkeypatch.setattr(engine_mod, "run_agent",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError("brainstorm called a second time"),
                            ))
        _install_agent_fakes(monkeypatch)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1,
                           max_node_executions=3)
        eng = Engine(str(wf), str(tmp_path), cfg)
        eng._load_workflow()
        eng._load_or_init_state()
        cont = eng._execute_step()

        assert cont is False
        assert eng.state.get("status") == "failed"
        err = eng.state.get("error") or {}
        assert err.get("code") == "LOOP_DETECTED_POST_BRAINSTORM"

    def test_brainstorm_returning_no_strategy_halts(self, tmp_path, monkeypatch):
        """Brainstorm agent returned success but empty new_strategy —
        engine halts with status=failed, code=BRAINSTORM_FAILED."""
        wf = _minimal_workflow(tmp_path)
        _seed_state(tmp_path, pc="start", exec_count=3)

        from camflow.backend.cam import engine as engine_mod

        def fake_run_agent(*a, **kw):
            return (
                {"status": "success", "summary": "no idea",
                 "state_updates": {}, "error": None},
                "a2",
                "file_appeared",
            )

        monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
        _install_agent_fakes(monkeypatch)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1,
                           max_node_executions=3)
        eng = Engine(str(wf), str(tmp_path), cfg)
        eng._load_workflow()
        eng._load_or_init_state()
        cont = eng._execute_step()

        assert cont is False
        assert eng.state.get("status") == "failed"
        assert (eng.state.get("error") or {}).get("code") == "BRAINSTORM_FAILED"
        # brainstorm_done_for was NOT marked — we didn't consume the rescue
        assert "start" not in (eng.state.get("brainstorm_done_for") or [])

    def test_brainstorm_exception_halts(self, tmp_path, monkeypatch):
        """If the brainstorm agent spawn itself raises, engine halts cleanly."""
        wf = _minimal_workflow(tmp_path)
        _seed_state(tmp_path, pc="start", exec_count=3)

        from camflow.backend.cam import engine as engine_mod

        def boom(*a, **kw):
            raise RuntimeError("camc exploded")

        monkeypatch.setattr(engine_mod, "run_agent", boom)
        _install_agent_fakes(monkeypatch)

        cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1,
                           max_node_executions=3)
        eng = Engine(str(wf), str(tmp_path), cfg)
        eng._load_workflow()
        eng._load_or_init_state()
        cont = eng._execute_step()

        assert cont is False
        assert eng.state.get("status") == "failed"
        assert (eng.state.get("error") or {}).get("code") == "BRAINSTORM_FAILED"
