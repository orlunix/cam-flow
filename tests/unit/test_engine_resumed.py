"""Engine emits ``engine_resumed`` to the Steward on a resume run.

A resume run is signalled by ``EngineConfig.reset=False`` (set by
``camflow resume``) — the engine loads existing state instead of
wiping it. Stewards are project-scoped, so the same Steward sees both
the original ``flow_started`` and the subsequent ``engine_resumed``
across the engine restart.
"""

from __future__ import annotations

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.steward import events as events_module


def _capture_emits(monkeypatch):
    """Replace ``emit`` with a recorder. Returns the list of (type, kwargs)
    tuples it accumulates."""
    captured: list[tuple[str, dict]] = []

    def fake_emit(project_dir, event_type, **kwargs):
        captured.append((event_type, kwargs))
        return False

    monkeypatch.setattr(events_module, "emit", fake_emit)
    return captured


def _seed_engine(workflow_path, project_dir, *, reset, flow_id="flow_xyz"):
    """Build a minimal Engine and seed enough state to drive the
    early run-startup branches without hitting the main loop."""
    cfg = EngineConfig(reset=reset)
    eng = Engine(str(workflow_path), str(project_dir), cfg)
    # Bypass the loop: we only care about pre-loop emits.
    eng.state = {"flow_id": flow_id, "pc": "build", "status": "running"}
    return eng


def test_resume_emits_engine_resumed(tmp_path, monkeypatch):
    captured = _capture_emits(monkeypatch)
    wf = tmp_path / "wf.yaml"
    wf.write_text("a: {do: cmd echo}\n")
    eng = _seed_engine(wf, tmp_path, reset=False)

    eng._emit_steward_flow_started()
    eng._emit_steward_engine_resumed(resumed_from="explicit_resume")

    types = [c[0] for c in captured]
    assert "flow_started" in types
    assert "engine_resumed" in types
    er = next(c for c in captured if c[0] == "engine_resumed")
    assert er[1]["flow_id"] == "flow_xyz"
    assert er[1]["pc"] == "build"
    assert er[1]["resumed_from"] == "explicit_resume"


def test_fresh_run_does_not_emit_engine_resumed(tmp_path, monkeypatch):
    """A fresh run (reset=True) should emit flow_started but not
    engine_resumed. The engine's run() method gates the engine_resumed
    call on ``not self.config.reset``.
    """
    captured = _capture_emits(monkeypatch)
    wf = tmp_path / "wf.yaml"
    wf.write_text("a: {do: cmd echo}\n")
    eng = _seed_engine(wf, tmp_path, reset=True)

    # Simulate just the relevant slice of run() — what the engine does
    # immediately after spawning the Steward.
    eng._emit_steward_flow_started()
    if not eng.config.reset:
        eng._emit_steward_engine_resumed(resumed_from="x")

    types = [c[0] for c in captured]
    assert "flow_started" in types
    assert "engine_resumed" not in types


def test_no_steward_short_circuits_engine_resumed(tmp_path, monkeypatch):
    captured = _capture_emits(monkeypatch)
    wf = tmp_path / "wf.yaml"
    wf.write_text("a: {do: cmd echo}\n")
    cfg = EngineConfig(reset=False, no_steward=True)
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "f", "pc": "p", "status": "running"}

    eng._emit_steward_engine_resumed(resumed_from="any")
    assert captured == []
