"""Resume: stop engine between steps, restart, verify correct continuation."""

import json
import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def _write_workflow(project_dir):
    wf = project_dir / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: cmd echo a
          next: b

        b:
          do: cmd echo b
          next: c

        c:
          do: cmd echo c
    """))
    return str(wf)


def test_resume_continues_from_saved_pc(tmp_path):
    wf = _write_workflow(tmp_path)
    camflow_dir = tmp_path / ".camflow"
    camflow_dir.mkdir(parents=True, exist_ok=True)

    # Pre-seed state as if the engine stopped after running node 'a'
    state = {
        "pc": "b",
        "status": "running",
        "retry_counts": {},
        "node_execution_count": {"a": 1},
        "lessons": [],
        "last_failure": None,
        "current_agent_id": None,
    }
    (camflow_dir / "state.json").write_text(json.dumps(state))

    cfg = EngineConfig(poll_interval=0, node_timeout=10, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done"

    # Trace should only have entries for b and c (a was skipped because state.pc=b)
    trace = camflow_dir / "trace.log"
    entries = [json.loads(l) for l in trace.read_text().strip().split("\n")]
    nodes = [e["node_id"] for e in entries]
    assert "a" not in nodes
    assert "b" in nodes
    assert "c" in nodes


def test_resume_done_is_noop(tmp_path):
    wf = _write_workflow(tmp_path)
    camflow_dir = tmp_path / ".camflow"
    camflow_dir.mkdir(parents=True, exist_ok=True)

    (camflow_dir / "state.json").write_text(json.dumps({
        "pc": None, "status": "done"
    }))

    cfg = EngineConfig(poll_interval=0, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()
    assert final["status"] == "done"

    # No trace should have been appended
    assert not (camflow_dir / "trace.log").exists()


def test_resume_workflow_missing_node(tmp_path):
    wf = _write_workflow(tmp_path)
    camflow_dir = tmp_path / ".camflow"
    camflow_dir.mkdir(parents=True, exist_ok=True)

    (camflow_dir / "state.json").write_text(json.dumps({
        "pc": "nonexistent",
        "status": "running",
        "retry_counts": {},
        "node_execution_count": {},
        "lessons": [],
        "last_failure": None,
        "current_agent_id": None,
    }))

    cfg = EngineConfig(poll_interval=0, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()
    assert final["status"] == "failed"
    assert final["error"]["code"] == "NODE_NOT_FOUND"


def test_resume_orphan_adopt_result(tmp_path, monkeypatch):
    """Engine had started an agent; result file exists; camc says agent still registered.
    Should ADOPT the result rather than re-execute."""
    wf = _write_workflow(tmp_path)
    camflow_dir = tmp_path / ".camflow"
    camflow_dir.mkdir(parents=True, exist_ok=True)

    (camflow_dir / "state.json").write_text(json.dumps({
        "pc": "start",
        "status": "running",
        "retry_counts": {},
        "node_execution_count": {},
        "lessons": [],
        "last_failure": None,
        "current_agent_id": "orph0001",
    }))

    # Result file exists
    (camflow_dir / "node-result.json").write_text(json.dumps({
        "status": "success",
        "summary": "orphan finished",
        "state_updates": {},
    }))

    # Mock camc as reporting completed
    from camflow.backend.cam import agent_runner, orphan_handler
    monkeypatch.setattr(orphan_handler, "_get_agent_status",
                        lambda _id: {"status": "completed", "state": "idle"})
    monkeypatch.setattr(agent_runner, "_cleanup_agent", lambda _id: None)

    # But 'a' in workflow is 'cmd echo a' — so it wouldn't have spawned a camc
    # agent. For this test we override a to be agent-typed so the orphan path is
    # meaningful. Rewrite the workflow:
    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: hi
          next: b

        b:
          do: cmd echo b
    """))

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
    eng = Engine(str(wf_path), str(tmp_path), cfg)
    final = eng.run()

    # Orphan result was adopted for 'start', then 'b' ran
    assert final["status"] == "done"

    trace = camflow_dir / "trace.log"
    entries = [json.loads(l) for l in trace.read_text().strip().split("\n")]
    # First entry should be the orphan adoption (event starts with "orphan_")
    first = entries[0]
    assert first["node_id"] == "start"
    assert first.get("event", "").startswith("orphan_")
