"""Workflow exceeds workflow_timeout → marked failed with WORKFLOW_TIMEOUT."""

import textwrap
import time

from camflow.backend.cam.engine import Engine, EngineConfig


def test_workflow_timeout_trips(tmp_path):
    # sleep 2 seconds per step, workflow_timeout=1 → should hit limit
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: cmd sleep 2
          next: done

        done:
          do: cmd echo ok
    """))

    cfg = EngineConfig(poll_interval=0, node_timeout=10, workflow_timeout=1, max_retries=1)
    eng = Engine(str(wf), str(tmp_path), cfg)
    start = time.time()
    final = eng.run()
    elapsed = time.time() - start

    assert final["status"] == "failed"
    assert final["error"]["code"] == "WORKFLOW_TIMEOUT"
    # Should exit shortly after the first node, not wait for 'done'
    assert elapsed < 10
