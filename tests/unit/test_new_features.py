"""Unit tests for the Week-1 batch: §5.1 context positioning, §5.2
observation masking, §5.3 tool scoping, §4.1 methodology router,
§4.2 escalation ladder, §6.1 git checkpoint.
"""

import subprocess

import pytest

from camflow.backend.cam import prompt_builder
from camflow.backend.cam.prompt_builder import (
    FENCE_CLOSE,
    FENCE_OPEN,
    build_prompt,
)
from camflow.engine import checkpoint as checkpoint_mod
from camflow.engine.checkpoint import checkpoint_after_success
from camflow.engine.escalation import (
    ESCALATION_PROMPTS,
    get_escalation_level,
    get_escalation_prompt,
)
from camflow.engine.methodology_router import (
    METHODOLOGIES,
    select_methodology,
    select_methodology_label,
)
from camflow.engine.state_enricher import (
    MAX_TEST_HISTORY,
    enrich_state,
    init_structured_fields,
)


# ---- §4.1 Methodology router -------------------------------------------


class TestMethodologyRouter:
    def test_fix_maps_to_rca(self):
        node = {"do": "agent claude", "with": "Fix the bug in calculator.py"}
        assert select_methodology_label("fix", node) == "rca"
        assert "RCA" in select_methodology("fix", node)

    def test_debug_maps_to_rca(self):
        node = {"do": "agent claude", "with": "Debug this error"}
        assert select_methodology_label("debug-step", node) == "rca"

    def test_build_maps_to_simplify_first(self):
        node = {"do": "agent claude", "with": "Build the release package"}
        assert select_methodology_label("build", node) == "simplify-first"
        assert "Simplify-first" in select_methodology("build", node)

    def test_research_maps_to_search_first(self):
        node = {"do": "agent claude", "with": "Research prior art for this algorithm"}
        assert select_methodology_label("research", node) == "search-first"

    def test_analyze_maps_to_search_first(self):
        node = {"do": "agent claude", "with": "Analyze the failure mode"}
        assert select_methodology_label("analyze", node) == "search-first"

    def test_design_maps_to_working_backwards(self):
        node = {"do": "agent claude", "with": "Design the new API surface"}
        assert select_methodology_label("design", node) == "working-backwards"

    def test_test_maps_to_systematic_coverage(self):
        node = {"do": "agent claude", "with": "Test the edge cases"}
        assert select_methodology_label("verify", node) == "systematic-coverage"

    def test_unknown_node_returns_none(self):
        node = {"do": "agent claude", "with": "Synthesize the report"}
        assert select_methodology_label("summarize", node) == "none"
        assert select_methodology("summarize", node) == ""

    def test_all_labels_have_hint_text(self):
        for label in ("rca", "simplify-first", "search-first",
                      "working-backwards", "systematic-coverage"):
            assert label in METHODOLOGIES
            assert METHODOLOGIES[label].startswith("Methodology:")


# ---- §4.2 Escalation ladder --------------------------------------------


class TestEscalationLadder:
    def test_level_zero_on_first_attempt(self):
        state = {"retry_counts": {}}
        assert get_escalation_level(state, "fix") == 0

    def test_level_increments_with_retries(self):
        state = {"retry_counts": {"fix": 1}}
        assert get_escalation_level(state, "fix") == 1

        state["retry_counts"]["fix"] = 2
        assert get_escalation_level(state, "fix") == 2

        state["retry_counts"]["fix"] = 3
        assert get_escalation_level(state, "fix") == 3

        state["retry_counts"]["fix"] = 4
        assert get_escalation_level(state, "fix") == 3

        state["retry_counts"]["fix"] = 5
        assert get_escalation_level(state, "fix") == 4

    def test_level_zero_prompt_is_empty(self):
        state = {"retry_counts": {}}
        assert get_escalation_prompt(state, "fix") == ""

    def test_prompts_differ_per_level(self):
        assert ESCALATION_PROMPTS[0] == ""
        assert "FUNDAMENTALLY DIFFERENT" in ESCALATION_PROMPTS[1]
        assert "DEEP DIVE" in ESCALATION_PROMPTS[2]
        assert "DIAGNOSTIC" in ESCALATION_PROMPTS[3]
        assert "ESCALATION" in ESCALATION_PROMPTS[4]

    def test_prompt_changes_at_each_level(self):
        state = {"retry_counts": {"fix": 0}}
        p0 = get_escalation_prompt(state, "fix")

        state["retry_counts"]["fix"] = 1
        p1 = get_escalation_prompt(state, "fix")

        state["retry_counts"]["fix"] = 2
        p2 = get_escalation_prompt(state, "fix")

        state["retry_counts"]["fix"] = 5
        p4 = get_escalation_prompt(state, "fix")

        assert p0 != p1
        assert p1 != p2
        assert p2 != p4
        assert p0 == ""


# ---- §5.1 Context positioning (HQ.1) ------------------------------------


class TestContextPositioning:
    def test_context_appears_before_task_header(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        state["iteration"] = 1
        state["lessons"] = ["L1"]
        p = build_prompt(
            "fix", {"do": "agent claude", "with": "TASK_MARKER"}, state,
        )
        # CONTEXT block opens before "Your task:" header
        assert p.index(FENCE_OPEN) < p.index("Your task:")
        # And before the role line
        assert p.index(FENCE_OPEN) < p.index("workflow node 'fix'")
        # Task body still at the end
        assert p.index("Your task:") < p.index("TASK_MARKER")

    def test_no_fence_still_keeps_task_last(self):
        p = build_prompt("n", {"do": "agent x", "with": "TASK"}, {})
        assert FENCE_OPEN not in p
        assert "Your task:" in p
        assert p.index("Your task:") < p.index("TASK")


# ---- §5.2 Observation masking -------------------------------------------


class TestObservationMasking:
    def _state(self):
        return init_structured_fields({"pc": "test", "status": "running"})

    def _fail_cmd(self, stdout):
        return {"status": "fail", "summary": "cmd failed",
                "output": {"stdout_tail": stdout},
                "state_updates": {}, "error": {"code": "CMD_FAIL"}}

    def test_first_round_populates_test_output_no_history(self):
        state = self._state()
        enrich_state(state, "test", self._fail_cmd("FAILED test_foo\n2 failed, 9 passed"))
        assert "FAILED test_foo" in state["test_output"]
        assert state["test_history"] == []

    def test_second_round_archives_first_summary(self):
        state = self._state()
        enrich_state(state, "test", self._fail_cmd("FAILED test_foo\n5 failed, 6 passed"))
        enrich_state(state, "test", self._fail_cmd("FAILED test_bar\n3 failed, 8 passed"))
        # test_output has the latest, test_history has the prior summary
        assert "test_bar" in state["test_output"]
        assert "test_foo" not in state["test_output"]
        assert len(state["test_history"]) == 1
        assert "5 failed" in state["test_history"][0]
        assert "iter" in state["test_history"][0]

    def test_history_capped(self):
        state = self._state()
        for i in range(MAX_TEST_HISTORY + 5):
            enrich_state(
                state, "test",
                self._fail_cmd(f"FAILED run_{i}\n{i} failed, 0 passed"),
            )
        # The first cap-1 entries survived the FIFO prune; cap entries total
        assert len(state["test_history"]) == MAX_TEST_HISTORY

    def test_success_does_not_archive_on_no_prior_output(self):
        state = self._state()
        result = {"status": "success", "summary": "ok",
                  "output": {}, "state_updates": {}, "error": None}
        enrich_state(state, "done", result)
        assert state["test_history"] == []


# ---- §5.3 Tool scoping --------------------------------------------------


class TestToolScoping:
    def test_tool_scope_rendered_when_set(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        node = {"do": "agent claude", "with": "fix it",
                "allowed_tools": ["Read", "Edit", "Write", "Bash"]}
        p = build_prompt("fix", node, state)
        assert "Tools you may use" in p
        assert "Read, Edit, Write, Bash" in p

    def test_no_tool_scope_line_when_unset(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        node = {"do": "agent claude", "with": "fix it"}
        p = build_prompt("fix", node, state)
        assert "Tools you may use" not in p


# ---- Methodology and escalation wiring into prompt_builder ---------------


class TestMethodologyAndEscalationInjection:
    def test_methodology_hint_in_prompt_for_fix(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        p = build_prompt(
            "fix", {"do": "agent claude", "with": "Fix divide()"}, state,
        )
        assert "Methodology:" in p
        assert "RCA" in p

    def test_no_methodology_for_unrecognized(self):
        state = init_structured_fields({"pc": "whatever", "status": "running"})
        p = build_prompt(
            "summarize", {"do": "agent claude", "with": "Summarize everything"},
            state,
        )
        assert "Methodology:" not in p

    def test_escalation_prompt_absent_at_level_zero(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        p = build_prompt(
            "fix", {"do": "agent claude", "with": "fix"}, state,
        )
        assert ESCALATION_PROMPTS[1] not in p
        assert ESCALATION_PROMPTS[2] not in p

    def test_escalation_prompt_appears_on_retry(self):
        state = init_structured_fields({"pc": "fix", "status": "running"})
        state["retry_counts"]["fix"] = 2
        p = build_prompt(
            "fix", {"do": "agent claude", "with": "fix"}, state,
        )
        assert "DEEP DIVE" in p


# ---- §6.1 Git checkpoint ------------------------------------------------


class TestGitCheckpoint:
    def test_runs_without_crash_on_non_git_dir(self, tmp_path):
        # Plain directory, no git; function should not raise
        result = checkpoint_after_success(
            str(tmp_path), "fix", 3, "fixed a bug",
        )
        # May be True (git init succeeded + commit) or False (git missing),
        # but must not raise
        assert isinstance(result, bool)

    def test_subprocess_error_is_swallowed(self, tmp_path, monkeypatch):
        """If every git call raises, checkpoint returns False but doesn't crash."""
        def exploding_run(*_a, **_kw):
            raise OSError("git not installed on this machine")

        monkeypatch.setattr(checkpoint_mod.subprocess, "run", exploding_run)
        result = checkpoint_after_success(
            str(tmp_path), "fix", 1, "summary",
        )
        assert result is False

    def test_runs_git_commands_in_order(self, tmp_path, monkeypatch):
        """When subprocess is mocked to succeed, git init+add+commit all run."""
        calls = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, cwd, capture_output=True, text=True, timeout=10):
            calls.append(args)
            return FakeProc()

        monkeypatch.setattr(checkpoint_mod.subprocess, "run", fake_run)
        ok = checkpoint_after_success(str(tmp_path), "fix", 5, "did a thing")
        assert ok is True

        # Three git commands, in order
        assert len(calls) == 3
        assert calls[0][:2] == ["git", "init"]
        assert calls[1][:3] == ["git", "add", "-A"]
        assert calls[2][0] == "git"
        assert calls[2][1] == "commit"
        # Commit message includes node_id and iteration
        msg_idx = calls[2].index("-m") + 1
        assert "fix" in calls[2][msg_idx]
        assert "iter 5" in calls[2][msg_idx]
        assert "did a thing" in calls[2][msg_idx]
