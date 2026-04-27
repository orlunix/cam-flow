"""Unit tests for camflow.steward.spawn.

We never invoke the real ``camc`` binary in unit tests — every test
either mocks the runner via the ``camc_runner`` parameter or works
purely on the boot-pack/persistence side.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow.registry import (
    get_agent,
    get_current_steward,
    load_registry,
)
from camflow.steward.spawn import (
    STEWARD_POINTER_FILE,
    _parse_agent_id,
    build_boot_pack,
    is_steward_alive,
    load_steward_pointer,
    spawn_steward,
)


# ---- _parse_agent_id ----------------------------------------------------


class TestParseAgentId:
    def test_lowercase_hex(self):
        assert _parse_agent_id("started agent 7c2a3f12") == "7c2a3f12"

    def test_uppercase_label(self):
        assert _parse_agent_id("ID: a1b2c3d4 ready") == "a1b2c3d4"

    def test_no_match(self):
        assert _parse_agent_id("camc returned without an id") is None


# ---- build_boot_pack ----------------------------------------------------


class TestBuildBootPack:
    def test_no_workflow_no_rationale(self, tmp_path):
        pack = build_boot_pack(tmp_path, None)
        assert "Steward agent" in pack
        assert "(no plan)" in pack
        assert "(no goal recorded" in pack

    def test_includes_goal_from_plan_request(self, tmp_path):
        camflow = tmp_path / ".camflow"
        camflow.mkdir()
        (camflow / "plan-request.txt").write_text("build calculator with tests\n")
        pack = build_boot_pack(tmp_path, None)
        assert "build calculator with tests" in pack

    def test_includes_rationale_excerpt(self, tmp_path):
        camflow = tmp_path / ".camflow"
        camflow.mkdir()
        (camflow / "plan-rationale.md").write_text(
            "# Why\n\nChose simplify-first because the goal is delivery."
        )
        pack = build_boot_pack(tmp_path, None)
        assert "simplify-first" in pack

    def test_long_rationale_truncated(self, tmp_path):
        camflow = tmp_path / ".camflow"
        camflow.mkdir()
        big = "x" * 10000
        (camflow / "plan-rationale.md").write_text(big)
        pack = build_boot_pack(tmp_path, None)
        assert "(truncated)" in pack

    def test_workflow_summary_lists_nodes(self, tmp_path):
        wf = tmp_path / "workflow.yaml"
        wf.write_text(
            "build:\n"
            "  do: cmd make build\n"
            "  next: test\n"
            "test:\n"
            "  do: cmd pytest\n"
        )
        pack = build_boot_pack(tmp_path, str(wf))
        assert "1. build" in pack
        assert "2. test" in pack


# ---- pointer file -------------------------------------------------------


class TestPointerFile:
    def test_load_returns_none_when_missing(self, tmp_path):
        assert load_steward_pointer(tmp_path) is None

    def test_load_after_spawn(self, tmp_path):
        spawn_steward(
            tmp_path,
            workflow_path=None,
            camc_runner=lambda *_a, **_k: "deadbeef",
        )
        ptr = load_steward_pointer(tmp_path)
        assert ptr is not None
        assert ptr["agent_id"] == "deadbeef"
        assert "spawned_at" in ptr


# ---- is_steward_alive ---------------------------------------------------


class TestIsStewardAlive:
    def test_no_pointer_means_dead(self, tmp_path):
        assert is_steward_alive(tmp_path) is False

    def test_alive_when_camc_returns_status(self, tmp_path):
        spawn_steward(
            tmp_path,
            workflow_path=None,
            camc_runner=lambda *_a, **_k: "deadbeef",
        )
        # camc says "running"
        assert is_steward_alive(
            tmp_path, camc_status=lambda _id: "running",
        ) is True

    def test_dead_when_camc_returns_none(self, tmp_path):
        spawn_steward(
            tmp_path,
            workflow_path=None,
            camc_runner=lambda *_a, **_k: "deadbeef",
        )
        assert is_steward_alive(
            tmp_path, camc_status=lambda _id: None,
        ) is False


# ---- spawn_steward — full integration with registry --------------------


class TestSpawnSteward:
    def test_spawn_writes_prompt_pointer_and_registry(self, tmp_path):
        captured: dict[str, str] = {}

        def fake_runner(name, project_dir, prompt):
            captured["name"] = name
            captured["project_dir"] = project_dir
            captured["prompt_excerpt"] = prompt[:200]
            return "7c2a3f12"

        agent_id = spawn_steward(
            tmp_path,
            workflow_path=None,
            camc_runner=fake_runner,
        )
        assert agent_id == "7c2a3f12"

        # Boot pack landed (Phase B: per-agent private dir).
        prompt_file = tmp_path / ".camflow" / "steward" / "prompt.txt"
        assert prompt_file.exists()
        body = prompt_file.read_text()
        assert "Steward agent" in body

        # Pointer file landed.
        ptr_path = tmp_path / ".camflow" / STEWARD_POINTER_FILE
        assert ptr_path.exists()
        ptr = json.loads(ptr_path.read_text())
        assert ptr["agent_id"] == "7c2a3f12"
        assert ptr["name"].startswith("steward-")

        # Registry has it as the current steward.
        agent = get_agent(tmp_path, "7c2a3f12")
        assert agent is not None
        assert agent["role"] == "steward"
        assert agent["status"] == "alive"
        current = get_current_steward(tmp_path)
        assert current["id"] == "7c2a3f12"

        # camc was invoked with a steward-* name.
        assert captured["name"].startswith("steward-")

    def test_spawn_emits_agent_spawned_trace(self, tmp_path):
        spawn_steward(
            tmp_path,
            workflow_path=None,
            camc_runner=lambda *_a, **_k: "abcdef00",
        )
        trace = (tmp_path / ".camflow" / "trace.log").read_text()
        events = [json.loads(l) for l in trace.splitlines() if l.strip()]
        spawned = [e for e in events if e["kind"] == "agent_spawned"]
        assert len(spawned) == 1
        assert spawned[0]["role"] == "steward"
        assert spawned[0]["agent_id"] == "abcdef00"
        assert spawned[0]["flow_id"] is None  # steward is project-scoped

    def test_spawn_propagates_runner_errors(self, tmp_path, capsys):
        def bad_runner(*_a, **_k):
            raise RuntimeError("camc segfaulted")

        with pytest.raises(RuntimeError, match="camc segfaulted"):
            spawn_steward(
                tmp_path,
                workflow_path=None,
                camc_runner=bad_runner,
            )
        # The user-facing message is on stderr so an operator can see it.
        assert "steward spawn failed" in capsys.readouterr().err

    def test_spawn_ids_are_unique_within_a_project(self, tmp_path):
        """Two spawns should not collide on the steward-<shortid> name.

        We don't enforce uniqueness ourselves (collision risk at 8 hex
        chars is 1 in 4 billion), but the name format guarantees the
        shape stays predictable.
        """
        ids = []

        def fake_runner(name, *_):
            ids.append(name)
            # Return distinct agent ids per call so the registry doesn't
            # complain about duplicates.
            return f"{len(ids):08x}"

        spawn_steward(
            tmp_path, workflow_path=None, camc_runner=fake_runner,
        )
        # Second spawn requires resetting the registry (otherwise the
        # current_steward_id pointer would still point at the prior id
        # which is fine, but the agents.json would have two steward
        # records — that's intentional for handoff scenarios).
        spawn_steward(
            tmp_path, workflow_path=None, camc_runner=fake_runner,
        )
        assert len(set(ids)) == 2
        assert all(n.startswith("steward-") for n in ids)

        # Both stewards are recorded in the registry.
        reg = load_registry(tmp_path)
        stewards = [a for a in reg["agents"] if a["role"] == "steward"]
        assert len(stewards) == 2
