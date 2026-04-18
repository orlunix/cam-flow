"""Unit tests for backend.cam.prompt_builder."""

from camflow.backend.cam.prompt_builder import (
    RESULT_CONTRACT,
    build_prompt,
    build_retry_prompt,
)


def test_template_substitution():
    node = {"do": "agent x", "with": "error: {{state.error}}"}
    p = build_prompt("fix", node, {"error": "divide by zero"})
    assert "divide by zero" in p
    assert "{{state.error}}" not in p


def test_includes_output_contract():
    p = build_prompt("n", {"do": "agent x", "with": "hi"}, {})
    assert RESULT_CONTRACT.strip() in p
    assert "node-result.json" in p


def test_no_lessons_block_when_empty():
    p = build_prompt("n", {"do": "agent x", "with": "hi"}, {})
    assert "Previous lessons" not in p


def test_lessons_block_when_present():
    state = {"lessons": ["lesson one", "lesson two"]}
    p = build_prompt("n", {"do": "agent x", "with": "hi"}, state)
    assert "Previous lessons" in p
    assert "1. lesson one" in p
    assert "2. lesson two" in p


def test_failure_block_when_last_failure_set():
    state = {
        "last_failure": {
            "node_id": "test",
            "summary": "3 tests failed",
            "stdout_tail": "FAILED test_foo",
            "stderr_tail": "E assertion",
            "attempt_count": 2,
        }
    }
    p = build_prompt("fix", {"do": "agent x", "with": "fix"}, state)
    assert "Last failure in node 'test'" in p
    assert "attempt 2" in p
    assert "FAILED test_foo" in p
    assert "E assertion" in p
    assert "DIFFERENT approach" in p


def test_retry_prompt_has_banner():
    state = {"last_failure": {"node_id": "fix", "summary": "prev", "attempt_count": 1}}
    p = build_retry_prompt(
        "fix", {"do": "agent x", "with": "fix"}, state,
        attempt=2, max_attempts=3, previous_summary="tried A",
    )
    assert "RETRY" in p
    assert "ATTEMPT 2 OF 3" in p
    assert "tried A" in p
    # Normal prompt also present
    assert "workflow node 'fix'" in p


def test_retry_prompt_without_previous_summary():
    p = build_retry_prompt("fix", {"do": "agent x"}, {}, attempt=2)
    assert "RETRY" in p
