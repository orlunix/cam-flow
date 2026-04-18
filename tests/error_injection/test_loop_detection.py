"""A workflow that loops forever should be stopped by max_node_executions."""

import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def test_infinite_loop_aborted(tmp_path):
    # start → loop → start → loop ...
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: cmd echo a
          next: loop

        loop:
          do: cmd echo b
          next: start
    """))

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1, max_node_executions=3)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "failed"
    assert final["error"]["code"] == "LOOP_DETECTED"
