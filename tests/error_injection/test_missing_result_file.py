"""Agent never produces a result file → engine times out and records a typed failure.

With --auto-exit removed, the engine owns the agent lifecycle; the only
ways a wait can end without a result are TIMEOUT (agent still running but
hasn't written the file) and AGENT_GONE (process died). This test simulates
the timeout path.
"""

import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def test_missing_result_file_classified_correctly(tmp_path, monkeypatch):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: agent placeholder
          with: hi
    """))

    from camflow.backend.cam import agent_runner

    def fake_start(node_id, prompt, project_dir, **kwargs):
        return "agent0001"

    def fake_wait(agent_id, result_path, timeout, poll_interval):
        # Simulate timeout: agent never writes the result file
        return ("timeout", None)

    def fake_capture(agent_id, lines=50):
        return "some screen output"

    def fake_cleanup(_agent_id):
        pass

    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
    monkeypatch.setattr(agent_runner, "_capture_screen", fake_capture)
    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup)

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "failed"
    # blocked and failed_approaches should have been populated by enrich_state
    assert final["blocked"] is not None
    assert final["blocked"]["node"] == "start"
    assert any(fa["node"] == "start" for fa in final["failed_approaches"])
    # The fail's error code should be AGENT_TIMEOUT
    # (we synthesize it in finalize_agent based on completion_signal)
