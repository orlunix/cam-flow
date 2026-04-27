"""Phase B: per-agent private directory layout (camflow.paths)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow import paths


class TestSteward:
    def test_steward_dir_created(self, tmp_path):
        d = paths.steward_dir(tmp_path)
        assert d.exists()
        assert d.name == "steward"
        assert d.parent.name == ".camflow"

    def test_per_file_helpers(self, tmp_path):
        assert paths.steward_prompt_path(tmp_path).name == "prompt.txt"
        assert paths.steward_summary_path(tmp_path).name == "summary.md"
        assert paths.steward_archive_path(tmp_path).name == "archive.md"
        assert paths.steward_inbox_path(tmp_path).name == "inbox.jsonl"
        assert paths.steward_session_log_path(tmp_path).name == "session.log"

    def test_archive_subdir_sanitises_timestamp(self, tmp_path):
        sub = paths.steward_archive_subdir(
            tmp_path, "steward-7c2a", "2026-04-26T10:00:00.123Z",
        )
        # No colons in the directory name.
        assert ":" not in sub.name
        assert sub.exists()


class TestFlow:
    def test_flow_dir_requires_flow_id(self, tmp_path):
        with pytest.raises(ValueError):
            paths.flow_dir(tmp_path, "")

    def test_flow_dir_created(self, tmp_path):
        d = paths.flow_dir(tmp_path, "flow_abc")
        assert d.exists()
        assert d.name == "flow_abc"
        assert d.parent.name == "flows"

    def test_flow_summary_path(self, tmp_path):
        p = paths.flow_summary_path(tmp_path, "flow_abc")
        assert p.name == "flow-summary.md"


class TestPlanner:
    def test_planner_dir_default(self, tmp_path):
        d = paths.planner_dir(tmp_path, "flow_xyz")
        assert d.name == "planner"

    def test_planner_dir_replan(self, tmp_path):
        d1 = paths.planner_dir(tmp_path, "flow_xyz", replan_n=1)
        d2 = paths.planner_dir(tmp_path, "flow_xyz", replan_n=2)
        assert d1.name == "planner-replan-1"
        assert d2.name == "planner-replan-2"
        assert d1 != d2

    def test_planner_files(self, tmp_path):
        f = "flow_q"
        assert paths.planner_prompt_path(tmp_path, f).name == "prompt.txt"
        assert paths.planner_request_path(tmp_path, f).name == "request.txt"
        assert paths.planner_draft_path(tmp_path, f).name == "workflow-draft.yaml"
        assert paths.planner_warnings_path(tmp_path, f).name == "warnings.txt"


class TestNodeAndAttempt:
    def test_node_dir(self, tmp_path):
        d = paths.node_dir(tmp_path, "flow_a", "build")
        assert d.exists()
        assert d.name == "build"
        assert d.parent.name == "nodes"
        assert d.parent.parent.name == "flow_a"

    def test_node_node_id_required(self, tmp_path):
        with pytest.raises(ValueError):
            paths.node_dir(tmp_path, "flow_a", "")

    def test_attempt_dir_1_indexed(self, tmp_path):
        d1 = paths.attempt_dir(tmp_path, "flow_a", "build", 1)
        d2 = paths.attempt_dir(tmp_path, "flow_a", "build", 2)
        assert d1.name == "1"
        assert d2.name == "2"

    def test_attempt_dir_rejects_zero(self, tmp_path):
        with pytest.raises(ValueError):
            paths.attempt_dir(tmp_path, "flow_a", "build", 0)

    def test_attempt_files(self, tmp_path):
        a = paths.attempt_dir(tmp_path, "flow_a", "build", 1)
        assert paths.attempt_agent_id_path(
            tmp_path, "flow_a", "build", 1
        ).name == "agent_id.txt"
        assert paths.attempt_progress_path(
            tmp_path, "flow_a", "build", 1
        ).name == "progress.json"
        assert paths.attempt_result_path(
            tmp_path, "flow_a", "build", 1
        ).name == "result.json"

    def test_node_winning_result_separate_from_attempts(self, tmp_path):
        winning = paths.node_winning_result_path(
            tmp_path, "flow_a", "build",
        )
        attempt = paths.attempt_result_path(
            tmp_path, "flow_a", "build", 1,
        )
        assert winning.name == "result.json"
        assert attempt.name == "result.json"
        # Different parents.
        assert winning.parent.name == "build"
        assert attempt.parent.name == "1"


class TestAttemptCounters:
    def test_latest_attempt_n_zero_when_empty(self, tmp_path):
        # Directory not yet populated.
        assert paths.latest_attempt_n(tmp_path, "flow_a", "build") == 0

    def test_latest_attempt_n_finds_max(self, tmp_path):
        paths.attempt_dir(tmp_path, "flow_a", "build", 1)
        paths.attempt_dir(tmp_path, "flow_a", "build", 2)
        paths.attempt_dir(tmp_path, "flow_a", "build", 5)
        assert paths.latest_attempt_n(tmp_path, "flow_a", "build") == 5

    def test_next_attempt_n(self, tmp_path):
        paths.attempt_dir(tmp_path, "flow_a", "build", 1)
        assert paths.next_attempt_n(tmp_path, "flow_a", "build") == 2


class TestAgentRunnerIntegration:
    """Worker spawn/finalize honors the new per-attempt layout when
    flow_id + attempt_n are passed; falls back to legacy paths when
    they're omitted (back-compat)."""

    def test_start_agent_writes_prompt_to_attempt_dir(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam import agent_runner

        # Mock subprocess so no real camc shell-out.
        class _Proc:
            def __init__(self):
                self.stdout = "agent abc12345 started\n"
                self.stderr = ""
                self.returncode = 0

        monkeypatch.setattr(
            agent_runner.subprocess, "run", lambda *a, **k: _Proc(),
        )
        monkeypatch.setattr(agent_runner, "_kick_prompt", lambda aid: None)

        agent_id = agent_runner.start_agent(
            "build", "PROMPT-CONTENT-HERE", str(tmp_path),
            flow_id="flow_xx", attempt_n=1,
        )
        assert agent_id == "abc12345"
        # Prompt landed in per-attempt private dir.
        attempt_prompt = paths.attempt_dir(
            tmp_path, "flow_xx", "build", 1,
        ) / "prompt.txt"
        assert attempt_prompt.exists()
        assert "PROMPT-CONTENT-HERE" in attempt_prompt.read_text()
        # agent_id recorded.
        aid_path = paths.attempt_agent_id_path(
            tmp_path, "flow_xx", "build", 1,
        )
        assert aid_path.read_text() == "abc12345"

    def test_start_agent_legacy_path_when_no_flow_id(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam import agent_runner

        class _Proc:
            stdout = "agent abcdef12 started\n"
            stderr = ""
            returncode = 0

        monkeypatch.setattr(
            agent_runner.subprocess, "run", lambda *a, **k: _Proc(),
        )
        monkeypatch.setattr(agent_runner, "_kick_prompt", lambda aid: None)

        agent_runner.start_agent(
            "build", "LEGACY-PROMPT", str(tmp_path),
        )
        legacy_prompt = tmp_path / ".camflow" / "node-prompt.txt"
        assert legacy_prompt.exists()
        assert "LEGACY-PROMPT" in legacy_prompt.read_text()

    def test_finalize_agent_archives_to_attempt_and_promotes_winning(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam import agent_runner
        from camflow.backend.cam.agent_runner import finalize_agent

        # Worker wrote a successful result to the canonical contract.
        cf = tmp_path / ".camflow"
        cf.mkdir()
        (cf / "node-result.json").write_text(
            json.dumps({"status": "success", "summary": "ok",
                        "state_updates": {}})
        )
        monkeypatch.setattr(
            agent_runner, "_cleanup_agent", lambda aid: None,
        )

        result = finalize_agent(
            "abc1", "file_appeared", str(tmp_path),
            flow_id="flow_xx", node_id="build", attempt_n=1,
        )
        assert result["status"] == "success"
        # Archived to attempt-private slot.
        attempt_result = paths.attempt_result_path(
            tmp_path, "flow_xx", "build", 1,
        )
        assert attempt_result.exists()
        assert json.loads(attempt_result.read_text())["status"] == "success"
        # Promoted to node-level winning slot.
        winning = paths.node_winning_result_path(
            tmp_path, "flow_xx", "build",
        )
        assert winning.exists()
        assert json.loads(winning.read_text())["status"] == "success"

    def test_finalize_agent_failed_result_not_promoted_to_winning(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam import agent_runner
        from camflow.backend.cam.agent_runner import finalize_agent

        cf = tmp_path / ".camflow"
        cf.mkdir()
        (cf / "node-result.json").write_text(
            json.dumps({"status": "fail", "summary": "oops",
                        "state_updates": {},
                        "error": {"code": "TEST"}})
        )
        monkeypatch.setattr(
            agent_runner, "_cleanup_agent", lambda aid: None,
        )

        result = finalize_agent(
            "abc1", "file_appeared", str(tmp_path),
            flow_id="flow_xx", node_id="build", attempt_n=1,
        )
        assert result["status"] == "fail"
        # Attempt slot has the failed result for audit.
        attempt_result = paths.attempt_result_path(
            tmp_path, "flow_xx", "build", 1,
        )
        assert attempt_result.exists()
        # Winning slot is NOT populated by a failed attempt.
        winning = paths.node_winning_result_path(
            tmp_path, "flow_xx", "build",
        )
        assert not winning.exists()


class TestStewardPrivateDir:
    def test_spawn_writes_to_steward_subdir(self, tmp_path, monkeypatch):
        from camflow.steward import spawn as spawn_module
        from camflow.steward.spawn import spawn_steward

        spawned = []
        agent_id = spawn_steward(
            tmp_path,
            workflow_path=None,
            spawned_by="test",
            camc_runner=lambda name, pdir, prompt:
                spawned.append((name, pdir)) or "stewardid1",
        )
        assert agent_id == "stewardid1"
        # Prompt lives at .camflow/steward/prompt.txt.
        prompt_path = paths.steward_prompt_path(tmp_path)
        assert prompt_path.exists()
        # NOT at the legacy location.
        legacy = tmp_path / ".camflow" / "steward-prompt.txt"
        assert not legacy.exists()


class TestPlannerPrivateDir:
    def test_generate_writes_planner_files_to_flow_subdir(self, tmp_path):
        from camflow.planner.agent_planner import generate_workflow_via_agent

        VALID_YAML = (
            "build:\n  do: cmd echo\n  next: done\ndone:\n  do: cmd echo\n"
        )

        def runner(name, project_dir, prompt):
            (Path(project_dir) / ".camflow" / "workflow.yaml").write_text(
                VALID_YAML
            )
            return "plannerid1"

        result = generate_workflow_via_agent(
            "test request",
            project_dir=str(tmp_path),
            flow_id="flow_pln",
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        # Planner private dir under flows/<flow>/planner/.
        prompt_p = paths.planner_prompt_path(tmp_path, "flow_pln")
        assert prompt_p.exists()
        request_p = paths.planner_request_path(tmp_path, "flow_pln")
        assert request_p.exists()
        assert "test request" in request_p.read_text()
        # NOT at legacy paths.
        assert not (tmp_path / ".camflow" / "planner-prompt.txt").exists()
        assert not (tmp_path / ".camflow" / "plan-request.txt").exists()
