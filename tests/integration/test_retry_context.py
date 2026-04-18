"""Integration test: retry flow with context injection.

Mocks agent_runner so we can script the sequence of results:
  attempt 1 → fail (node_fail)
  attempt 2 → fail (node_fail)
  attempt 3 → success

Verifies that:
  - last_failure is populated on each failure
  - retry counts are tracked
  - on final success, last_failure is cleared and retry counts reset
  - trace records attempt/is_retry/retry_mode correctly
"""

import json
import textwrap

import pytest

from camflow.backend.cam import engine as engine_mod
from camflow.backend.cam.engine import Engine, EngineConfig


def _write_workflow(project_dir):
    path = project_dir / "workflow.yaml"
    path.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: Fix it
          next: done

        done:
          do: cmd echo ok
    """))
    return str(path)


def test_retry_with_task_mode_injects_context(tmp_path, monkeypatch):
    results_queue = [
        {
            "status": "fail",
            "summary": "attempt 1 failed",
            "output": {"stdout_tail": "ATTEMPT1_OUTPUT"},
            "state_updates": {},
            "error": {"code": "NODE_FAIL"},
        },
        {
            "status": "fail",
            "summary": "attempt 2 failed",
            "output": {"stdout_tail": "ATTEMPT2_OUTPUT"},
            "state_updates": {},
            "error": {"code": "NODE_FAIL"},
        },
        {
            "status": "success",
            "summary": "finally worked",
            "output": {},
            "state_updates": {"new_lesson": "always check edge cases"},
            "error": None,
        },
    ]
    prompts_seen = []
    call_index = [0]

    def fake_start_agent(node_id, prompt, project_dir):
        prompts_seen.append(prompt)
        return f"agent{call_index[0]:08x}"

    def fake_wait(agent_id, result_path, timeout, poll_interval):
        return ("file_appeared", None)

    def fake_finalize(agent_id, completion_signal, project_dir, cleanup=True):
        r = results_queue[call_index[0]]
        call_index[0] += 1
        return r

    # Patch the agent_runner functions referenced inside Engine._run_node
    from camflow.backend.cam import agent_runner
    monkeypatch.setattr(agent_runner, "start_agent", fake_start_agent)
    monkeypatch.setattr(agent_runner, "_wait_for_completion", fake_wait)
    monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)

    wf = _write_workflow(tmp_path)
    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=3, max_node_executions=10)
    eng = Engine(wf, str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done", final
    # Three agent calls happened
    assert len(prompts_seen) == 3

    # First prompt: no RETRY banner
    assert "RETRY" not in prompts_seen[0]

    # Second prompt: RETRY banner + ATTEMPT1 context
    assert "RETRY" in prompts_seen[1]
    assert "ATTEMPT 2" in prompts_seen[1]
    assert "ATTEMPT1_OUTPUT" in prompts_seen[1]

    # Third prompt: RETRY banner + ATTEMPT2 context (latest)
    assert "RETRY" in prompts_seen[2]
    assert "ATTEMPT 3" in prompts_seen[2]
    assert "ATTEMPT2_OUTPUT" in prompts_seen[2]

    # After final success, retry counter is reset and last_failure cleared
    assert final["retry_counts"].get("start", 0) == 0
    assert final["last_failure"] is None

    # Lesson captured
    assert "always check edge cases" in final["lessons"]

    # Trace has attempts 1, 2, 3 for node 'start'
    trace = tmp_path / ".camflow" / "trace.log"
    entries = [json.loads(l) for l in trace.read_text().strip().split("\n")]
    start_entries = [e for e in entries if e["node_id"] == "start"]
    attempts = [e["attempt"] for e in start_entries]
    assert attempts == [1, 2, 3]
    assert start_entries[0]["is_retry"] is False
    assert start_entries[1]["is_retry"] is True
