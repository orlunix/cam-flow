"""Integration test: lessons accumulate across iterations and inject into future prompts."""

import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def _write_workflow(project_dir):
    path = project_dir / "workflow.yaml"
    # 3 nodes: each adds a lesson
    path.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: first
          next: b

        b:
          do: agent claude
          with: second
          next: c

        c:
          do: agent claude
          with: third
    """))
    return str(path)


def test_lessons_accumulate_and_inject(tmp_path, monkeypatch):
    prompts = []
    results = [
        {"status": "success", "summary": "a done", "output": {},
         "state_updates": {"new_lesson": "lesson one"}, "error": None},
        {"status": "success", "summary": "b done", "output": {},
         "state_updates": {"new_lesson": "lesson two"}, "error": None},
        {"status": "success", "summary": "c done", "output": {},
         "state_updates": {"new_lesson": "lesson one"},  # duplicate — should be deduped
         "error": None},
    ]
    idx = [0]

    from camflow.backend.cam import agent_runner

    def fake_start(node_id, prompt, project_dir):
        prompts.append(prompt)
        return f"agent{idx[0]:08x}"

    def fake_wait(agent_id, result_path, timeout, poll_interval):
        return ("file_appeared", None)

    def fake_finalize(agent_id, completion_signal, project_dir, cleanup=True):
        r = results[idx[0]]
        idx[0] += 1
        return r

    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_completion", fake_wait)
    monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
    eng = Engine(_write_workflow(tmp_path), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done"
    assert final["lessons"] == ["lesson one", "lesson two"]  # deduped

    # prompts[0] has no lessons yet
    assert "lesson one" not in prompts[0]

    # prompts[1] has lesson one
    assert "lesson one" in prompts[1]
    assert "lesson two" not in prompts[1]

    # prompts[2] has both
    assert "lesson one" in prompts[2]
    assert "lesson two" in prompts[2]
