"""Dry-run mode walks the workflow without executing."""

import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


def test_dry_run_prints_nodes_no_execution(tmp_path, capsys):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: cmd echo "would run"
          next: done

        done:
          do: cmd echo "terminal"
    """))

    cfg = EngineConfig(dry_run=True, max_retries=1)
    eng = Engine(str(wf), str(tmp_path), cfg)
    rc = eng.run()
    assert rc == 0

    out = capsys.readouterr().out
    assert "start" in out
    assert "done" in out
    assert "cmd echo" in out

    # No state.json or trace.log should be written by a dry run
    assert not (tmp_path / ".camflow" / "state.json").exists()
    assert not (tmp_path / ".camflow" / "trace.log").exists()
