"""Unit tests for agent cleanup.

Cleanup is now registry-driven: ``cleanup_workers_of_flow`` reads
``.camflow/agents.json`` and kills only the workers belonging to the
specified flow. Stewards (project-scoped) and unrelated agents on the
same host are never touched, even when their tmux session name shares
the ``camflow-`` prefix. The old broad-sweep helpers are kept as
no-ops for legacy tests that monkey-patch them.

Coverage:
  - ``CAMC_BIN`` resolved at import (Fix 1, unchanged).
  - ``_list_camflow_agent_ids`` honors the prefix filter (Fix 3 — kept
    only because the helper is still useful for diagnostics).
  - ``cleanup_workers_of_flow`` — only kills role=worker, matching
    flow_id, status=alive; skips stewards, mismatched flows, completed
    workers, agents outside the registry; safe with missing args.
  - ``cleanup_all_camflow_agents`` and ``kill_existing_camflow_agents``
    are deprecated no-ops.
  - Engine ``_cleanup_on_exit`` calls the new registry-scoped helper
    with this flow's ``flow_id``.
"""

import json

import pytest

from camflow.backend.cam import agent_runner
from camflow.backend.cam.agent_runner import (
    CAMC_BIN,
    _list_camflow_agent_ids,
    cleanup_all_camflow_agents,
    cleanup_workers_of_flow,
    kill_existing_camflow_agents,
)
from camflow.registry import register_agent


# ---- Fix 1: PATH resolution -------------------------------------------


def test_camc_bin_is_resolved_at_import():
    """CAMC_BIN must be either an absolute path (via shutil.which) or
    the literal 'camc' fallback. Never empty / None."""
    assert isinstance(CAMC_BIN, str)
    assert CAMC_BIN  # truthy


# ---- _list_camflow_agent_ids (still useful for diagnostics) ----------


def _fake_list_response(*camflow_ids, other_ids=None):
    """Return a fake `camc --json list` payload as bytes-like text."""
    other_ids = other_ids or []
    payload = []
    for aid in camflow_ids:
        payload.append({"id": aid, "task": {"name": f"camflow-{aid}"}})
    for aid in other_ids:
        payload.append({"id": aid, "task": {"name": f"unrelated-{aid}"}})
    return json.dumps(payload)


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_list_camflow_agent_ids_filters_by_prefix(monkeypatch):
    payload = _fake_list_response(
        "aaa11111", "bbb22222", other_ids=["zzz99999"],
    )

    def fake_run(args, capture_output=True, text=True, timeout=10):
        if args[1:3] == ["--json", "list"]:
            return _Proc(stdout=payload)
        return _Proc()

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    ids = _list_camflow_agent_ids()
    assert set(ids) == {"aaa11111", "bbb22222"}
    assert "zzz99999" not in ids


def test_list_camflow_agent_ids_handles_camc_failure(monkeypatch):
    def fake_run(args, capture_output=True, text=True, timeout=10):
        return _Proc(stdout="", returncode=1)

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    assert _list_camflow_agent_ids() == []


def test_list_camflow_agent_ids_handles_exception(monkeypatch):
    def fake_run(*_a, **_kw):
        raise OSError("camc not found")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    assert _list_camflow_agent_ids() == []


# ---- cleanup_workers_of_flow — registry-driven cleanup ---------------


def _seed_registry(project_dir, *agents):
    for a in agents:
        register_agent(project_dir, a)


def test_cleanup_workers_of_flow_kills_matching_alive_worker(
    monkeypatch, tmp_path,
):
    killed = []

    def fake_cleanup(aid):
        killed.append(aid)

    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup)

    _seed_registry(
        tmp_path,
        {
            "id": "camflow-build-aaa1",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_001",
        },
    )

    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == ["camflow-build-aaa1"]


def test_cleanup_workers_of_flow_never_kills_stewards(monkeypatch, tmp_path):
    """The whole point of the rewrite: Stewards are project-scoped and
    must never be killed by the engine's cleanup sweep."""
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )

    _seed_registry(
        tmp_path,
        {
            "id": "steward-7c2a",
            "role": "steward",
            "status": "alive",
            # Steward has no flow_id (project-scoped) but even if it
            # did, role!=worker means it must be skipped.
        },
        {
            "id": "camflow-build-aaa1",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_001",
        },
    )

    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == ["camflow-build-aaa1"]
    assert "steward-7c2a" not in killed


def test_cleanup_workers_of_flow_skips_other_flows(monkeypatch, tmp_path):
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )

    _seed_registry(
        tmp_path,
        {
            "id": "camflow-build-aaa1",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_001",
        },
        {
            "id": "camflow-build-bbb2",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_002",
        },
    )

    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == ["camflow-build-aaa1"]


def test_cleanup_workers_of_flow_skips_terminal_workers(
    monkeypatch, tmp_path,
):
    """Don't re-kill workers that already finished or were killed."""
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )

    _seed_registry(
        tmp_path,
        {
            "id": "camflow-build-done1",
            "role": "worker",
            "status": "completed",
            "flow_id": "flow_001",
        },
        {
            "id": "camflow-build-fail1",
            "role": "worker",
            "status": "failed",
            "flow_id": "flow_001",
        },
        {
            "id": "camflow-build-kill1",
            "role": "worker",
            "status": "killed",
            "flow_id": "flow_001",
        },
        {
            "id": "camflow-build-alive1",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_001",
        },
    )

    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == ["camflow-build-alive1"]


def test_cleanup_workers_of_flow_no_op_without_flow_id(
    monkeypatch, tmp_path,
):
    """Defensive: no flow scope known → kill nothing."""
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )

    _seed_registry(
        tmp_path,
        {
            "id": "camflow-build-aaa1",
            "role": "worker",
            "status": "alive",
            "flow_id": "flow_001",
        },
    )

    cleanup_workers_of_flow(str(tmp_path), None)
    cleanup_workers_of_flow(str(tmp_path), "")
    assert killed == []


def test_cleanup_workers_of_flow_no_op_without_project_dir(monkeypatch):
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )
    cleanup_workers_of_flow(None, "flow_001")
    cleanup_workers_of_flow("", "flow_001")
    assert killed == []


def test_cleanup_workers_of_flow_swallows_registry_load_error(
    monkeypatch, tmp_path,
):
    """If the registry can't be read, log nothing and kill nothing."""
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )

    def explode(*_a, **_kw):
        raise OSError("disk gone")

    import camflow.registry as registry
    monkeypatch.setattr(registry, "list_agents", explode)
    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == []


def test_cleanup_workers_of_flow_skips_unknown_agents(
    monkeypatch, tmp_path,
):
    """Agents on the host but NOT in this project's registry must not
    be touched. This is the exact regression we want to never see again
    — the parent ``camflow-dev`` agent is not in the registry."""
    killed = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent", lambda aid: killed.append(aid),
    )
    # Registry intentionally empty.
    cleanup_workers_of_flow(str(tmp_path), "flow_001")
    assert killed == []


# ---- deprecated helpers — must be no-ops -----------------------------


def test_cleanup_all_is_now_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(
        agent_runner.subprocess, "run",
        lambda *a, **k: called.append(a) or _Proc(),
    )
    cleanup_all_camflow_agents()
    assert called == []


def test_kill_existing_is_now_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(
        agent_runner.subprocess, "run",
        lambda *a, **k: called.append(a) or _Proc(),
    )
    kill_existing_camflow_agents(except_id="orphan99")
    kill_existing_camflow_agents()
    assert called == []


# ---- Engine integration: _cleanup_on_exit ----------------------------


def test_engine_cleanup_on_exit_removes_current_and_calls_workers_sweep(
    monkeypatch, tmp_path,
):
    """``_cleanup_on_exit`` must (1) clean the current_agent_id and
    (2) sweep the flow's workers — but only via the new
    registry-scoped helper, not the old broad sweep."""
    from camflow.backend.cam.engine import Engine

    cleanup_calls = []

    def fake_cleanup_agent(aid):
        cleanup_calls.append(("cleanup_agent", aid))

    def fake_workers_sweep(project_dir, flow_id):
        cleanup_calls.append(("workers_sweep", project_dir, flow_id))

    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup_agent)
    monkeypatch.setattr(
        agent_runner, "cleanup_workers_of_flow", fake_workers_sweep,
    )

    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": "abc12345", "flow_id": "flow_001"}
    eng.state_path = str(tmp_path / "state.json")
    eng.project_dir = str(tmp_path)
    eng._cleanup_on_exit()

    actions = [c[0] for c in cleanup_calls]
    assert "cleanup_agent" in actions
    assert "workers_sweep" in actions
    # The sweep was called with this engine's flow scope, not a
    # host-wide sweep.
    sweep = next(c for c in cleanup_calls if c[0] == "workers_sweep")
    assert sweep[1] == str(tmp_path)
    assert sweep[2] == "flow_001"
    # And current_agent_id was cleared in state
    assert eng.state["current_agent_id"] is None


def test_engine_cleanup_on_exit_handles_no_current_agent(
    monkeypatch, tmp_path,
):
    from camflow.backend.cam.engine import Engine

    cleanup_calls = []
    monkeypatch.setattr(
        agent_runner, "_cleanup_agent",
        lambda aid: cleanup_calls.append(("cleanup_agent", aid)),
    )
    monkeypatch.setattr(
        agent_runner, "cleanup_workers_of_flow",
        lambda pd, fid: cleanup_calls.append(("workers_sweep", pd, fid)),
    )

    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": None, "flow_id": "flow_002"}
    eng.state_path = str(tmp_path / "state.json")
    eng.project_dir = str(tmp_path)
    eng._cleanup_on_exit()

    actions = [c[0] for c in cleanup_calls]
    assert "cleanup_agent" not in actions
    assert "workers_sweep" in actions


def test_engine_cleanup_on_exit_swallows_exceptions(monkeypatch, tmp_path):
    from camflow.backend.cam.engine import Engine

    def explode(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_runner, "_cleanup_agent", explode)
    monkeypatch.setattr(agent_runner, "cleanup_workers_of_flow", explode)

    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": "deadbeef", "flow_id": "flow_001"}
    eng.state_path = str(tmp_path / "state.json")
    eng.project_dir = str(tmp_path)
    # Must not raise even if every cleanup helper fails
    eng._cleanup_on_exit()
