"""Agent returns but doesn't write the result file → engine detects PARSE_ERROR."""

import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def test_missing_result_file_classified_correctly(tmp_path, monkeypatch):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: hi
    """))

    from camflow.backend.cam import agent_runner

    def fake_start(node_id, prompt, project_dir):
        return "agent0001"

    def fake_wait(agent_id, result_path, timeout, poll_interval):
        # Report terminal status, but never create the result file
        return ("status_terminal", "completed")

    def fake_capture(agent_id, lines=50):
        return "some screen output"

    def fake_cleanup(_agent_id):
        pass

    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_completion", fake_wait)
    monkeypatch.setattr(agent_runner, "_capture_screen", fake_capture)
    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup)

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "failed"
    # last_failure should have been populated with PARSE_ERROR context
    assert final["last_failure"] is not None
