"""Unit tests for backend.cam.prompt_builder — fenced injection variant."""

from camflow.backend.cam.prompt_builder import (
    FENCE_CLOSE,
    FENCE_OPEN,
    RESULT_CONTRACT,
    build_prompt,
    build_retry_prompt,
)


class TestTemplateSubstitution:
    def test_state_var_substitution(self):
        node = {"do": "agent x", "with": "error: {{state.error}}"}
        p = build_prompt("fix", node, {"error": "divide by zero"})
        assert "divide by zero" in p
        assert "{{state.error}}" not in p


class TestOutputContract:
    def test_includes_output_contract(self):
        p = build_prompt("n", {"do": "agent x", "with": "hi"}, {})
        assert RESULT_CONTRACT.strip() in p
        assert "node-result.json" in p
        assert "files_touched" in p  # from updated contract


class TestEmptyState:
    def test_no_fence_when_state_has_nothing(self):
        p = build_prompt("n", {"do": "agent x", "with": "hi"}, {})
        assert FENCE_OPEN not in p
        assert FENCE_CLOSE not in p

    def test_fence_rendered_when_state_has_content(self):
        p = build_prompt(
            "n", {"do": "agent x", "with": "hi"},
            {"iteration": 2, "lessons": ["L1"]},
        )
        assert FENCE_OPEN in p
        assert FENCE_CLOSE in p


class TestFencedSections:
    def test_iteration_rendered(self):
        p = build_prompt(
            "fix", {"do": "agent x", "with": "go"},
            {"iteration": 3},
        )
        assert "Iteration: 3" in p
        assert "this node: fix" in p

    def test_active_task(self):
        p = build_prompt(
            "fix", {"do": "agent x", "with": "go"},
            {"iteration": 1, "active_task": "Fix calculator bugs"},
        )
        assert "Active task: Fix calculator bugs" in p

    def test_completed_rendered(self):
        state = {
            "iteration": 2,
            "completed": [
                {"node": "fix", "action": "fixed divide", "file": "calc.py", "lines": "L16-18"},
                {"node": "fix", "action": "fixed average", "file": "calc.py"},
            ],
        }
        p = build_prompt("fix", {"do": "agent x", "with": "continue"}, state)
        assert "Completed so far:" in p
        assert "fixed divide" in p
        assert "calc.py L16-18" in p
        assert "fixed average" in p

    def test_test_output(self):
        state = {
            "iteration": 2,
            "test_output": "FAILED test_foo\nFAILED test_bar\n2 failed, 9 passed",
        }
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "Current test / cmd output" in p
        assert "FAILED test_foo" in p

    def test_key_files(self):
        state = {
            "iteration": 1,
            "active_state": {"key_files": ["a.py", "b.py"]},
        }
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "Key files: a.py, b.py" in p

    def test_lessons(self):
        state = {"iteration": 1, "lessons": ["check edges", "prefer builtins"]}
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "Lessons learned:" in p
        assert "- check edges" in p
        assert "- prefer builtins" in p

    def test_failed_approaches(self):
        state = {
            "iteration": 3,
            "failed_approaches": [
                {"node": "fix", "approach": "tried X", "iteration": 1},
                {"node": "fix", "approach": "tried Y", "iteration": 2},
            ],
        }
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "Previously failed approaches" in p
        assert "tried X" in p
        assert "tried Y" in p

    def test_blocked(self):
        state = {
            "iteration": 1,
            "blocked": {"node": "test", "reason": "3 tests failing"},
        }
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "blocked" in p.lower()
        assert "3 tests failing" in p

    def test_next_steps(self):
        state = {"iteration": 1, "next_steps": ["fix factorial", "fix power"]}
        p = build_prompt("fix", {"do": "agent x", "with": "go"}, state)
        assert "Next steps" in p
        assert "- fix factorial" in p


class TestFenceIsolation:
    def test_task_comes_after_fence(self):
        state = {"iteration": 1, "lessons": ["L"]}
        p = build_prompt(
            "fix", {"do": "agent x", "with": "TASK_MARKER"},
            state,
        )
        assert FENCE_CLOSE in p
        assert p.index(FENCE_CLOSE) < p.index("TASK_MARKER")

    def test_task_framing_word_present(self):
        p = build_prompt(
            "fix", {"do": "agent x", "with": "DO_THIS"},
            {"iteration": 1, "lessons": ["L"]},
        )
        assert "Your task:" in p


class TestRetryPrompt:
    def test_retry_banner(self):
        state = {
            "iteration": 2,
            "failed_approaches": [{"node": "fix", "approach": "tried X", "iteration": 1}],
        }
        p = build_retry_prompt(
            "fix", {"do": "agent x", "with": "go"}, state,
            attempt=2, max_attempts=3, previous_summary="tried X",
        )
        assert "RETRY" in p
        assert "ATTEMPT 2 OF 3" in p
        assert "tried X" in p
        assert "workflow node 'fix'" in p

    def test_retry_without_previous_summary(self):
        p = build_retry_prompt("fix", {"do": "agent x"}, {}, attempt=2)
        assert "RETRY" in p
