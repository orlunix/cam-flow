"""``flows_witnessed`` is the project-scoped Steward's record of every
flow_id it saw during its lifetime. The engine appends to it once per
flow_started event (design §12).
"""

from __future__ import annotations

from camflow.registry import (
    append_flow_to_steward,
    get_current_steward,
    register_agent,
    set_current_steward,
)


def _seed_steward(project_dir, agent_id="steward-7c2a", with_flows=None):
    register_agent(
        project_dir,
        {
            "id": agent_id,
            "role": "steward",
            "status": "alive",
            "spawned_at": "2026-04-26T10:00:00Z",
            "spawned_by": "test",
            "flows_witnessed": list(with_flows or []),
        },
    )
    set_current_steward(project_dir, agent_id)


# ---- happy path --------------------------------------------------------


def test_appends_first_flow(tmp_path):
    _seed_steward(tmp_path)
    assert append_flow_to_steward(tmp_path, "flow_001") is True
    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == ["flow_001"]


def test_appends_multiple_flows_in_order(tmp_path):
    _seed_steward(tmp_path)
    append_flow_to_steward(tmp_path, "flow_001")
    append_flow_to_steward(tmp_path, "flow_002")
    append_flow_to_steward(tmp_path, "flow_003")
    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == ["flow_001", "flow_002", "flow_003"]


# ---- idempotence -------------------------------------------------------


def test_duplicate_flow_is_noop(tmp_path):
    """Engine restart on the same flow_id (resume path) must not
    accumulate duplicates."""
    _seed_steward(tmp_path, with_flows=["flow_001"])
    assert append_flow_to_steward(tmp_path, "flow_001") is False
    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == ["flow_001"]


def test_repeated_calls_after_other_flows_are_idempotent(tmp_path):
    _seed_steward(tmp_path)
    append_flow_to_steward(tmp_path, "flow_a")
    append_flow_to_steward(tmp_path, "flow_b")
    # Now flow_a comes back (e.g. resume) — must remain a single entry.
    assert append_flow_to_steward(tmp_path, "flow_a") is False
    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == ["flow_a", "flow_b"]


# ---- defensive paths ---------------------------------------------------


def test_no_steward_returns_false(tmp_path):
    """Project has never spawned a Steward (e.g. --no-steward run).
    The append must be a quiet no-op rather than raising."""
    assert append_flow_to_steward(tmp_path, "flow_001") is False


def test_empty_flow_id_returns_false(tmp_path):
    _seed_steward(tmp_path)
    assert append_flow_to_steward(tmp_path, "") is False
    assert append_flow_to_steward(tmp_path, None) is False
    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == []


def test_current_steward_id_pointing_to_missing_record(tmp_path):
    """If current_steward_id points to a record that has been removed
    somehow, the call returns False rather than raising."""
    # Manually set up a corrupted state by directly editing registry.
    from camflow.registry.agents import load_registry, _save
    reg = load_registry(tmp_path)
    reg["current_steward_id"] = "ghost-7c2a"  # doesn't exist
    _save(tmp_path, reg)
    assert append_flow_to_steward(tmp_path, "flow_001") is False


# ---- engine integration ------------------------------------------------


def test_engine_emit_flow_started_appends_to_witnessed(
    tmp_path, monkeypatch,
):
    """``Engine._emit_steward_flow_started`` calls
    append_flow_to_steward as a side effect."""
    from camflow.backend.cam.engine import Engine, EngineConfig

    _seed_steward(tmp_path)

    # Stub emit() so it doesn't try to send anything.
    from camflow.steward import events as events_module
    monkeypatch.setattr(
        events_module, "emit", lambda *a, **k: False,
    )

    wf = tmp_path / "wf.yaml"
    wf.write_text("a:\n  do: cmd echo\n")
    cfg = EngineConfig()
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "flow_engine_001"}

    eng._emit_steward_flow_started()

    sw = get_current_steward(tmp_path)
    assert sw["flows_witnessed"] == ["flow_engine_001"]


def test_engine_emit_flow_started_with_no_steward_no_op(
    tmp_path, monkeypatch,
):
    """If the project has no Steward, the append is silently skipped."""
    from camflow.backend.cam.engine import Engine, EngineConfig

    from camflow.steward import events as events_module
    monkeypatch.setattr(
        events_module, "emit", lambda *a, **k: False,
    )

    wf = tmp_path / "wf.yaml"
    wf.write_text("a:\n  do: cmd echo\n")
    cfg = EngineConfig()
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "flow_001"}

    # Must not raise.
    eng._emit_steward_flow_started()
