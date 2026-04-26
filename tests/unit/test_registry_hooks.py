"""Unit tests for camflow.registry.hooks."""

import json
from pathlib import Path

import pytest

from camflow.registry import (
    get_agent,
    list_agents,
    load_registry,
    on_agent_finalized,
    on_agent_handoff_archived,
    on_agent_killed,
    on_agent_spawned,
    register_agent,
)


def _read_trace(project_dir):
    path = Path(project_dir) / ".camflow" / "trace.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- on_agent_spawned --------------------------------------------------


class TestOnAgentSpawned:
    def test_registers_and_traces(self, tmp_path):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-build-a1b2c3",
            spawned_by="engine (flow_001 step 1)",
            flow_id="flow_001",
            node_id="build",
            prompt_file=".camflow/node-prompt.txt",
        )

        # Registry record landed.
        agent = get_agent(tmp_path, "camflow-build-a1b2c3")
        assert agent is not None
        assert agent["role"] == "worker"
        assert agent["status"] == "alive"
        assert agent["flow_id"] == "flow_001"
        assert agent["node_id"] == "build"
        assert agent["spawned_by"] == "engine (flow_001 step 1)"

        # Trace event landed.
        trace = _read_trace(tmp_path)
        assert len(trace) == 1
        e = trace[0]
        assert e["kind"] == "agent_spawned"
        assert e["actor"] == "engine"
        assert e["flow_id"] == "flow_001"
        assert e["agent_id"] == "camflow-build-a1b2c3"
        assert e["role"] == "worker"
        assert e["node_id"] == "build"

    def test_multiple_agents_appear_in_order(self, tmp_path):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-build-a1",
            spawned_by="engine",
            flow_id="f1",
            node_id="build",
        )
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-test-b2",
            spawned_by="engine",
            flow_id="f1",
            node_id="test",
        )

        ids = [a["id"] for a in list_agents(tmp_path)]
        assert ids == ["camflow-build-a1", "camflow-test-b2"]

        trace = _read_trace(tmp_path)
        assert len(trace) == 2
        assert all(e["kind"] == "agent_spawned" for e in trace)

    def test_extra_fields_merged(self, tmp_path):
        on_agent_spawned(
            tmp_path,
            role="steward",
            agent_id="steward-7c2a",
            spawned_by="camflow run (smooth)",
            extra={"memory_files": [".camflow/steward-summary.md"]},
        )
        agent = get_agent(tmp_path, "steward-7c2a")
        assert agent["memory_files"] == [".camflow/steward-summary.md"]


# ---- on_agent_finalized ------------------------------------------------


class TestOnAgentFinalized:
    def _spawn(self, tmp_path, agent_id="camflow-build-a1"):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id=agent_id,
            spawned_by="engine",
            flow_id="f1",
            node_id="build",
        )

    def test_success_marks_completed(self, tmp_path):
        self._spawn(tmp_path)
        on_agent_finalized(
            tmp_path,
            agent_id="camflow-build-a1",
            result={"status": "success", "summary": "ok"},
            flow_id="f1",
            duration_ms=1234,
            completion_signal="file_appeared",
        )
        agent = get_agent(tmp_path, "camflow-build-a1")
        assert agent["status"] == "completed"
        assert agent["duration_ms"] == 1234
        assert agent["completion_signal"] == "file_appeared"
        assert "completed_at" in agent

        # Last trace entry is agent_completed.
        trace = _read_trace(tmp_path)
        assert trace[-1]["kind"] == "agent_completed"
        assert trace[-1]["agent_id"] == "camflow-build-a1"
        assert trace[-1]["duration_ms"] == 1234

    def test_fail_marks_failed_and_captures_error_code(self, tmp_path):
        self._spawn(tmp_path)
        on_agent_finalized(
            tmp_path,
            agent_id="camflow-build-a1",
            result={
                "status": "fail",
                "summary": "compilation failed",
                "error": {"code": "NODE_FAIL", "reason": "syntax error"},
            },
            flow_id="f1",
            completion_signal="file_appeared",
        )
        agent = get_agent(tmp_path, "camflow-build-a1")
        assert agent["status"] == "failed"
        assert agent["error_code"] == "NODE_FAIL"

        trace = _read_trace(tmp_path)
        assert trace[-1]["kind"] == "agent_failed"
        assert trace[-1]["error_code"] == "NODE_FAIL"

    def test_finalize_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError):
            on_agent_finalized(
                tmp_path,
                agent_id="ghost",
                result={"status": "success"},
                flow_id="f1",
            )


# ---- on_agent_killed ---------------------------------------------------


class TestOnAgentKilled:
    def test_kill_records_killer_and_reason(self, tmp_path):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-fix-a1",
            spawned_by="engine",
            flow_id="f1",
            node_id="fix",
        )
        on_agent_killed(
            tmp_path,
            agent_id="camflow-fix-a1",
            killed_by="steward-7c2a",
            reason="stuck on compaction",
            flow_id="f1",
            via="camflow ctl kill-worker",
        )

        agent = get_agent(tmp_path, "camflow-fix-a1")
        assert agent["status"] == "killed"
        assert agent["killed_by"] == "steward-7c2a"
        assert agent["killed_reason"] == "stuck on compaction"

        trace = _read_trace(tmp_path)
        assert trace[-1]["kind"] == "agent_killed"
        assert trace[-1]["actor"] == "steward-7c2a"
        assert trace[-1]["via"] == "camflow ctl kill-worker"


# ---- on_agent_handoff_archived ----------------------------------------


class TestOnAgentHandoffArchived:
    def test_archives_old_steward(self, tmp_path):
        register_agent(
            tmp_path,
            {
                "id": "steward-7c2a",
                "role": "steward",
                "status": "alive",
                "spawned_at": "2026-04-26T10:00:00Z",
                "spawned_by": "camflow run",
            },
        )
        register_agent(
            tmp_path,
            {
                "id": "steward-7c2a-v2",
                "role": "steward",
                "status": "alive",
                "spawned_at": "2026-04-26T11:00:00Z",
                "spawned_by": "engine handoff",
            },
        )

        on_agent_handoff_archived(
            tmp_path,
            agent_id="steward-7c2a",
            successor_id="steward-7c2a-v2",
            memory_carried=[
                ".camflow/steward-summary.md",
                ".camflow/steward-archive.md",
            ],
        )

        old = get_agent(tmp_path, "steward-7c2a")
        assert old["status"] == "handoff_archived"
        assert old["successor_id"] == "steward-7c2a-v2"

        trace = _read_trace(tmp_path)
        e = [x for x in trace if x["kind"] == "handoff_completed"][0]
        assert e["from_agent"] == "steward-7c2a"
        assert e["to_agent"] == "steward-7c2a-v2"
        assert e["flow_id"] is None  # project-level event
        assert ".camflow/steward-summary.md" in e["memory_carried"]


# ---- registry + trace stay consistent ---------------------------------


def test_registry_and_trace_in_lockstep(tmp_path):
    """Each lifecycle call writes one registry update + one trace entry."""
    on_agent_spawned(
        tmp_path,
        role="worker",
        agent_id="w-1",
        spawned_by="engine",
        flow_id="f1",
        node_id="n1",
    )
    on_agent_spawned(
        tmp_path,
        role="worker",
        agent_id="w-2",
        spawned_by="engine",
        flow_id="f1",
        node_id="n2",
    )
    on_agent_finalized(
        tmp_path,
        agent_id="w-1",
        result={"status": "success"},
        flow_id="f1",
    )
    on_agent_killed(
        tmp_path,
        agent_id="w-2",
        killed_by="steward-7c2a",
        reason="stuck",
        flow_id="f1",
    )

    # Registry has 2 agents in their final states.
    agents = load_registry(tmp_path)["agents"]
    assert len(agents) == 2
    by_id = {a["id"]: a for a in agents}
    assert by_id["w-1"]["status"] == "completed"
    assert by_id["w-2"]["status"] == "killed"

    # Trace has 4 events (2 spawn + 1 complete + 1 kill).
    trace = _read_trace(tmp_path)
    assert len(trace) == 4
    kinds = [e["kind"] for e in trace]
    assert kinds == [
        "agent_spawned",
        "agent_spawned",
        "agent_completed",
        "agent_killed",
    ]
