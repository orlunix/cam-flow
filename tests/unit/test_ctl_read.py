"""Unit tests for the read-only ctl verbs (camflow.cli_entry.ctl_read)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow.cli_entry import ctl as ctl_module
from camflow.cli_entry.ctl import dispatch
from camflow.cli_entry.ctl_read import _register_all
from camflow.registry import on_agent_spawned


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test starts fresh: only the read-only verbs registered."""
    saved = dict(ctl_module.VERBS)
    ctl_module.VERBS.clear()
    _register_all()
    yield
    ctl_module.VERBS.clear()
    ctl_module.VERBS.update(saved)


def _camflow_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".camflow"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- read-state --------------------------------------------------------


class TestReadState:
    def test_pretty_print(self, tmp_path, capsys):
        d = _camflow_dir(tmp_path)
        (d / "state.json").write_text(json.dumps({"pc": "build", "status": "running"}))
        rc = dispatch("read-state", [], project_dir=str(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert '"pc": "build"' in out
        assert "\n  " in out  # indented

    def test_json_compact(self, tmp_path, capsys):
        d = _camflow_dir(tmp_path)
        (d / "state.json").write_text(json.dumps({"pc": "build"}))
        rc = dispatch("read-state", ["--json"], project_dir=str(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == '{"pc": "build"}'

    def test_missing_file_returns_1(self, tmp_path, capsys):
        rc = dispatch("read-state", [], project_dir=str(tmp_path))
        assert rc == 1
        assert "no state.json" in capsys.readouterr().err


# ---- read-trace --------------------------------------------------------


class TestReadTrace:
    def _seed(self, tmp_path: Path) -> None:
        d = _camflow_dir(tmp_path)
        (d / "trace.log").write_text(
            "\n".join(
                json.dumps(e)
                for e in (
                    {"kind": "step", "step": 1, "node_id": "build"},
                    {"kind": "agent_spawned", "agent_id": "a1", "actor": "engine"},
                    {"kind": "step", "step": 2, "node_id": "test"},
                    {"kind": "agent_completed", "agent_id": "a1"},
                )
            ) + "\n"
        )

    def test_default_tail_20(self, tmp_path, capsys):
        self._seed(tmp_path)
        rc = dispatch("read-trace", [], project_dir=str(tmp_path))
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 4

    def test_tail_2_returns_last_two(self, tmp_path, capsys):
        self._seed(tmp_path)
        rc = dispatch(
            "read-trace", ["--tail", "2"], project_dir=str(tmp_path),
        )
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        last = json.loads(lines[-1])
        assert last["kind"] == "agent_completed"

    def test_kind_filter(self, tmp_path, capsys):
        self._seed(tmp_path)
        rc = dispatch(
            "read-trace", ["--kind", "step"], project_dir=str(tmp_path),
        )
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        assert all(json.loads(l)["kind"] == "step" for l in lines)

    def test_kind_filter_multiple(self, tmp_path, capsys):
        self._seed(tmp_path)
        rc = dispatch(
            "read-trace",
            ["--kind", "agent_spawned", "--kind", "agent_completed"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2

    def test_missing_returns_1(self, tmp_path):
        rc = dispatch("read-trace", [], project_dir=str(tmp_path))
        assert rc == 1


# ---- read-events -------------------------------------------------------


class TestReadEvents:
    def test_prints_tail(self, tmp_path, capsys):
        d = _camflow_dir(tmp_path)
        (d / "steward-events.jsonl").write_text(
            json.dumps({"type": "node_done", "step": 1}) + "\n"
            + json.dumps({"type": "node_done", "step": 2}) + "\n"
        )
        rc = dispatch(
            "read-events", ["--tail", "1"], project_dir=str(tmp_path),
        )
        assert rc == 0
        line = capsys.readouterr().out.strip()
        assert json.loads(line)["step"] == 2

    def test_missing_returns_1(self, tmp_path):
        rc = dispatch("read-events", [], project_dir=str(tmp_path))
        assert rc == 1


# ---- read-rationale ----------------------------------------------------


class TestReadRationale:
    def test_prints_file(self, tmp_path, capsys):
        d = _camflow_dir(tmp_path)
        body = "# Plan rationale\n\nChose simplify-first because..."
        (d / "plan-rationale.md").write_text(body)
        rc = dispatch("read-rationale", [], project_dir=str(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert body in out

    def test_missing_returns_1(self, tmp_path):
        rc = dispatch("read-rationale", [], project_dir=str(tmp_path))
        assert rc == 1


# ---- read-registry -----------------------------------------------------


class TestReadRegistry:
    def test_table_view_with_agents(self, tmp_path, capsys):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-build-a1",
            spawned_by="engine",
            flow_id="flow_001",
            node_id="build",
        )
        rc = dispatch("read-registry", [], project_dir=str(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Project:" in out
        assert "Agents: 1" in out
        assert "camflow-build-a1" in out
        assert "worker" in out
        assert "flow_001" in out

    def test_json_mode(self, tmp_path, capsys):
        on_agent_spawned(
            tmp_path,
            role="worker",
            agent_id="camflow-build-a1",
            spawned_by="engine",
            flow_id="flow_001",
            node_id="build",
        )
        rc = dispatch(
            "read-registry", ["--json"], project_dir=str(tmp_path),
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["agents"][0]["id"] == "camflow-build-a1"

    def test_missing_returns_1(self, tmp_path):
        rc = dispatch("read-registry", [], project_dir=str(tmp_path))
        assert rc == 1

    def test_empty_registry_prints_count(self, tmp_path, capsys):
        # Create an empty registry file
        d = _camflow_dir(tmp_path)
        (d / "agents.json").write_text(json.dumps({
            "version": 1,
            "project_dir": str(tmp_path),
            "current_steward_id": None,
            "agents": [],
        }))
        rc = dispatch("read-registry", [], project_dir=str(tmp_path))
        assert rc == 0
        assert "Agents: 0" in capsys.readouterr().out


# ---- all-verbs registered after auto-load ------------------------------


def test_all_read_verbs_registered():
    names = set(ctl_module.VERBS.keys())
    assert {
        "read-state", "read-trace", "read-events",
        "read-rationale", "read-registry",
    } <= names
