"""Unit tests for engine.dsl."""

import textwrap

import pytest

from camflow.engine.dsl import (
    classify_do,
    load_workflow,
    validate_node,
    validate_workflow,
)


def write_yaml(tmp_path, content):
    p = tmp_path / "workflow.yaml"
    p.write_text(textwrap.dedent(content))
    return str(p)


def test_load_workflow(tmp_path):
    p = write_yaml(tmp_path, """
        start:
          do: cmd echo hi
    """)
    wf = load_workflow(p)
    assert "start" in wf
    assert wf["start"]["do"] == "cmd echo hi"


def test_validate_node_requires_do():
    ok, errs = validate_node("n", {"with": "hi"})
    assert not ok
    assert any("do" in e for e in errs)


def test_validate_node_unknown_field():
    ok, errs = validate_node("n", {"do": "cmd x", "foo": "bar"})
    assert not ok
    assert any("unknown" in e.lower() for e in errs)


def test_validate_node_invalid_empty_do():
    """DSL v2: empty or whitespace `do` is invalid (was: unknown executor)."""
    ok, errs = validate_node("n", {"do": "   "})
    assert not ok
    assert any("invalid" in e.lower() for e in errs)


def test_validate_node_free_text_is_inline_prompt():
    """DSL v2: any string without a keyword prefix is a valid inline prompt."""
    ok, errs = validate_node("n", {"do": "banana x"})
    assert ok, errs


def test_validate_node_bare_keyword_rejected():
    """DSL v2: a keyword with no body is invalid (agent with no name etc.)."""
    for do in ("agent", "agent ", "shell", "skill"):
        ok, errs = validate_node("n", {"do": do})
        assert not ok, f"expected {do!r} to fail"


def test_validate_node_transitions_must_have_if_goto():
    ok, errs = validate_node("n", {"do": "cmd x", "transitions": [{"if": "fail"}]})
    assert not ok
    assert any("transition" in e for e in errs)


def test_validate_workflow_accepts_any_first_node():
    """Workflows no longer need a literal 'start' node — the engine
    uses whichever node is declared first in YAML order."""
    ok, errs = validate_workflow({"foo": {"do": "cmd x"}})
    assert ok, errs


def test_validate_workflow_rejects_empty():
    ok, errs = validate_workflow({})
    assert not ok
    assert any("no nodes" in e for e in errs)


def test_validate_workflow_dangling_goto():
    wf = {
        "start": {"do": "cmd x", "next": "missing"},
    }
    ok, errs = validate_workflow(wf)
    assert not ok
    assert any("does not exist" in e for e in errs)


def test_validate_workflow_happy_path():
    wf = {
        "start": {"do": "cmd x", "next": "done"},
        "done": {"do": "cmd y"},
    }
    ok, errs = validate_workflow(wf)
    assert ok, errs


# ---------- DSL v2: classify_do ----------

def test_classify_shell_and_cmd_alias():
    """`shell` is the new spelling; `cmd` is still accepted."""
    assert classify_do("shell pytest -v") == ("shell", "pytest -v")
    assert classify_do("cmd pytest -v") == ("shell", "pytest -v")


def test_classify_agent_named_any_name():
    """classify_do treats every `agent <name>` the same — parser doesn't
    care whether the name resolves to a real ~/.claude/agents/<name>.md
    file. The loader enforces file existence at run time; `classify_do`
    just splits the token.
    """
    assert classify_do("agent rtl-debugger") == ("agent", "rtl-debugger")
    assert classify_do("agent placeholder") == ("agent", "placeholder")
    assert classify_do("subagent rtl-debugger") == ("agent", "rtl-debugger")


def test_classify_skill():
    assert classify_do("skill rtl-trace") == ("skill", "rtl-trace")


def test_classify_inline_prompt():
    """Free text with no keyword prefix is an inline prompt."""
    kind, body = classify_do("Fix the bug in calculator.py")
    assert kind == "inline"
    assert body == "Fix the bug in calculator.py"


def test_classify_invalid():
    assert classify_do("")[0] == "invalid"
    assert classify_do("   ")[0] == "invalid"
    assert classify_do("agent ")[0] == "invalid"
    assert classify_do(None)[0] == "invalid"


# ---------- DSL v2: node field validation ----------

def test_validate_node_model_field():
    ok, errs = validate_node("n", {"do": "agent placeholder", "model": "claude"})
    assert ok, errs
    ok, errs = validate_node("n", {"do": "agent placeholder", "model": 3})
    assert not ok


def test_validate_node_preflight_field():
    ok, errs = validate_node(
        "n", {"do": "agent placeholder", "preflight": "test -f simv"}
    )
    assert ok, errs
    ok, errs = validate_node("n", {"do": "agent placeholder", "preflight": ""})
    assert not ok
