"""Integration test: cmd-only workflow end-to-end.

No camc, no agents — just cmd nodes to validate the engine's state machine,
transitions, persistence, and trace writing.
"""

import json
import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def _write_workflow(project_dir, content):
    path = project_dir / "workflow.yaml"
    path.write_text(textwrap.dedent(content))
    return str(path)


def test_cmd_only_workflow_end_to_end(tmp_path):
    wf = _write_workflow(tmp_path, """
        start:
          do: cmd echo "step 1"
          next: check

        check:
          do: cmd test -f {marker}
          transitions:
            - if: fail
              goto: create
            - if: success
              goto: done

        create:
          do: cmd touch {marker}
          next: check

        done:
          do: cmd echo "all done"
    """.format(marker=str(tmp_path / "marker.txt")))

    cfg = EngineConfig(poll_interval=0, node_timeout=30, workflow_timeout=60, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done"

    # State persisted
    state_path = tmp_path / ".camflow" / "state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["status"] == "done"

    # Trace has the expected sequence (filter to per-step entries —
    # trace.log is now tagged-union and also contains agent_spawned /
    # event_emitted / flow_started / etc.)
    trace_path = tmp_path / ".camflow" / "trace.log"
    lines = trace_path.read_text().strip().split("\n")
    entries = [json.loads(l) for l in lines]
    steps = [e for e in entries if e.get("kind", "step") == "step"]
    sequence = [e["node_id"] for e in steps]
    # Expected: start → check (fail) → create → check (pass) → done
    assert sequence == ["start", "check", "create", "check", "done"]
    final_step = steps[-1]
    assert final_step["node_id"] == "done"
    assert final_step["transition"]["workflow_status"] == "done"


def test_cmd_branch_if_fail_taken(tmp_path):
    wf = _write_workflow(tmp_path, """
        start:
          do: cmd false
          transitions:
            - if: fail
              goto: recover
            - if: success
              goto: done

        recover:
          do: cmd echo "recovered"
          next: done

        done:
          do: cmd echo "ok"
    """)
    cfg = EngineConfig(poll_interval=0, node_timeout=10, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()

    # 'start' fails → recover (success) → done
    assert final["status"] == "done"

    trace_path = tmp_path / ".camflow" / "trace.log"
    entries = [json.loads(l) for l in trace_path.read_text().strip().split("\n")]
    steps = [e for e in entries if e.get("kind", "step") == "step"]
    nodes = [e["node_id"] for e in steps]
    assert "start" in nodes
    assert "recover" in nodes


def test_cmd_output_capture_available_to_next_node(tmp_path):
    """A cmd that produces stdout makes it available via state.last_cmd_output."""
    wf = _write_workflow(tmp_path, """
        start:
          do: cmd echo CAPTURED_MARKER
          next: verify

        verify:
          do: cmd bash -c 'echo "{{state.last_cmd_output}}" | grep CAPTURED_MARKER'
    """)
    cfg = EngineConfig(poll_interval=0, node_timeout=10, max_retries=1)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()
    # If capture flowed through, verify exits 0 and workflow is 'done'
    assert final["status"] == "done"
