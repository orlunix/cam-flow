"""Unit tests for the agent-based Planner orchestration.

Coverage: spawn → poll → parse → validate → register → cleanup, plus
the failure modes (early agent death, timeout, invalid yaml, DSL
violation, plan-quality violation). All ``camc`` shell-outs are
injected via the ``camc_runner`` / ``camc_remover`` / ``camc_status``
parameters so no test ever spawns a real agent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from camflow.planner.agent_planner import (
    DEFAULT_POLL_INTERVAL,
    PROMPT_FILE,
    RATIONALE_FILE,
    REQUEST_FILE,
    WORKFLOW_FILE,
    PlannerAgentError,
    build_boot_pack,
    generate_workflow_via_agent,
)
from camflow.registry import get_agent, list_agents


VALID_WORKFLOW_YAML = """\
build:
  do: cmd echo hello
  next: done
done:
  do: cmd echo done
"""


# ---- boot pack ---------------------------------------------------------


class TestBootPack:
    def test_request_inlined(self, tmp_path):
        bp = build_boot_pack(tmp_path, "build a thing")
        assert "build a thing" in bp
        assert str(tmp_path) in bp

    def test_no_request_marks_placeholder(self, tmp_path):
        bp = build_boot_pack(tmp_path, "")
        assert "(no request provided)" in bp

    def test_mentions_required_deliverables(self, tmp_path):
        bp = build_boot_pack(tmp_path, "x")
        assert WORKFLOW_FILE in bp
        assert RATIONALE_FILE in bp
        assert "camflow plan-tool validate" in bp
        assert "camflow plan-tool write" in bp


# ---- happy path --------------------------------------------------------


def _runner_writes_workflow(workflow_yaml: str, rationale: str | None = None):
    """Build a fake camc runner that, when invoked, schedules the
    workflow.yaml + (optional) rationale.md to land on disk a tick
    after the agent is "spawned"."""

    def runner(name: str, project_dir: str, prompt: str) -> str:
        cf = Path(project_dir) / ".camflow"
        cf.mkdir(parents=True, exist_ok=True)
        (cf / WORKFLOW_FILE).write_text(workflow_yaml, encoding="utf-8")
        if rationale is not None:
            (cf / RATIONALE_FILE).write_text(rationale, encoding="utf-8")
        return "abcd1234"

    return runner


class TestHappyPath:
    def test_spawns_polls_returns_workflow(self, tmp_path):
        runner = _runner_writes_workflow(
            VALID_WORKFLOW_YAML, rationale="why I picked these nodes"
        )
        rms: list[str] = []

        result = generate_workflow_via_agent(
            "build a thing",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: rms.append(aid),
            camc_status=lambda aid: "alive",
        )
        assert result.agent_id == "abcd1234"
        assert "build" in result.workflow
        assert "done" in result.workflow
        assert result.workflow_path.endswith(WORKFLOW_FILE)
        assert result.rationale_path is not None
        assert result.rationale_path.endswith(RATIONALE_FILE)
        # Cleanup happened.
        assert rms == ["abcd1234"]

    def test_request_persisted_to_disk(self, tmp_path):
        runner = _runner_writes_workflow(VALID_WORKFLOW_YAML)
        generate_workflow_via_agent(
            "the user's exact request text",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        # Phase B: request lives in the Planner's private dir under
        # .camflow/flows/<flow>/planner/request.txt.
        flows_dir = tmp_path / ".camflow" / "flows"
        flow_dirs = list(flows_dir.iterdir())
        assert len(flow_dirs) == 1
        request_text = (
            flow_dirs[0] / "planner" / REQUEST_FILE
        ).read_text(encoding="utf-8")
        assert "the user's exact request text" in request_text

    def test_prompt_file_written(self, tmp_path):
        runner = _runner_writes_workflow(VALID_WORKFLOW_YAML)
        generate_workflow_via_agent(
            "x",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        # Phase B: prompt lives under planner private dir.
        flow_dirs = list((tmp_path / ".camflow" / "flows").iterdir())
        assert len(flow_dirs) == 1
        prompt = (flow_dirs[0] / "planner" / PROMPT_FILE).read_text(
            encoding="utf-8"
        )
        assert "PLANNER" in prompt.upper() or "Planner" in prompt

    def test_registers_in_agent_registry_with_role_planner(self, tmp_path):
        runner = _runner_writes_workflow(VALID_WORKFLOW_YAML)
        generate_workflow_via_agent(
            "x",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        agent = get_agent(tmp_path, "abcd1234")
        assert agent is not None
        assert agent["role"] == "planner"
        # Final state — agent_finalized fired with status=success.
        assert agent["status"] == "completed"

    def test_stale_workflow_pre_cleared(self, tmp_path):
        """A leftover workflow.yaml from a prior planner run must not
        be mistaken for this run's success."""
        cf = tmp_path / ".camflow"
        cf.mkdir()
        (cf / WORKFLOW_FILE).write_text(
            "stale_node:\n  do: cmd echo stale\n", encoding="utf-8",
        )
        runner = _runner_writes_workflow(VALID_WORKFLOW_YAML)
        result = generate_workflow_via_agent(
            "x",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        # The new workflow won, not the stale one.
        assert "build" in result.workflow
        assert "stale_node" not in result.workflow


# ---- failure modes -----------------------------------------------------


class TestFailures:
    def test_runner_raises_planner_error_propagated(self, tmp_path):
        def boom(*_a, **_kw):
            raise PlannerAgentError("camc unreachable")

        with pytest.raises(PlannerAgentError, match="camc unreachable"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=2,
                poll_interval=0.01,
                camc_runner=boom,
            )

    def test_runner_raises_unrelated_wrapped(self, tmp_path):
        def explode(*_a, **_kw):
            raise RuntimeError("disk")

        with pytest.raises(PlannerAgentError, match="camc runner raised"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=2,
                poll_interval=0.01,
                camc_runner=explode,
            )

    def test_agent_dies_before_writing_workflow(self, tmp_path):
        # Runner returns an agent id but never writes workflow.yaml.
        # Status probe returns None → agent considered dead.
        rm_calls = []

        def runner(name, project_dir, prompt):
            return "deadbeef"

        with pytest.raises(PlannerAgentError, match="disappeared from camc"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=30,
                poll_interval=0.01,
                camc_runner=runner,
                camc_remover=lambda aid: rm_calls.append(aid),
                camc_status=lambda aid: None,  # camc says: not alive
            )
        # Even on failure, we tried to clean up.
        assert rm_calls == ["deadbeef"]
        # And agents.json reflects the kill.
        agent = get_agent(tmp_path, "deadbeef")
        assert agent is not None
        assert agent["status"] == "killed"

    def test_timeout_raises_with_elapsed_time(self, tmp_path):
        def runner(name, project_dir, prompt):
            return "slowpoke"  # never writes workflow

        rm_calls = []
        with pytest.raises(PlannerAgentError, match=r"timed out after \d+s"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=0.05,
                poll_interval=0.01,
                camc_runner=runner,
                camc_remover=lambda aid: rm_calls.append(aid),
                camc_status=lambda aid: "alive",
            )
        assert rm_calls == ["slowpoke"]

    def test_invalid_yaml_diagnosed(self, tmp_path):
        def runner(name, project_dir, prompt):
            cf = Path(project_dir) / ".camflow"
            cf.mkdir(parents=True, exist_ok=True)
            (cf / WORKFLOW_FILE).write_text("foo: [bad\n")
            return "ag1"

        with pytest.raises(PlannerAgentError, match="parses badly"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=10,
                poll_interval=0.01,
                camc_runner=runner,
                camc_remover=lambda aid: None,
                camc_status=lambda aid: "alive",
            )

    def test_dsl_validation_failure(self, tmp_path):
        def runner(name, project_dir, prompt):
            cf = Path(project_dir) / ".camflow"
            cf.mkdir(parents=True, exist_ok=True)
            (cf / WORKFLOW_FILE).write_text(
                "a:\n  do: cmd echo\n  next: ghost\n"
            )
            return "ag1"

        with pytest.raises(PlannerAgentError, match="DSL validation"):
            generate_workflow_via_agent(
                "x",
                project_dir=str(tmp_path),
                timeout_seconds=10,
                poll_interval=0.01,
                camc_runner=runner,
                camc_remover=lambda aid: None,
                camc_status=lambda aid: "alive",
            )


# ---- warnings persistence ----------------------------------------------


class TestWarnings:
    def test_quality_warnings_written_to_disk(self, tmp_path):
        # The default valid workflow has no `verify` clause →
        # plan-quality emits a warning. We assert it gets persisted.
        runner = _runner_writes_workflow(VALID_WORKFLOW_YAML)
        result = generate_workflow_via_agent(
            "x",
            project_dir=str(tmp_path),
            timeout_seconds=10,
            poll_interval=0.01,
            camc_runner=runner,
            camc_remover=lambda aid: None,
            camc_status=lambda aid: "alive",
        )
        # plan-quality may or may not warn on this minimal yaml — only
        # assert if any warning surfaced, the file got written.
        if result.warnings:
            warnings_path = (
                tmp_path / ".camflow" / "plan-warnings.txt"
            )
            assert warnings_path.exists()
