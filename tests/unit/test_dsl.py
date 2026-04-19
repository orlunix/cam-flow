"""Unit tests for engine.dsl."""

import textwrap

import pytest

from camflow.engine.dsl import load_workflow, validate_node, validate_workflow


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


def test_validate_node_invalid_executor():
    ok, errs = validate_node("n", {"do": "banana x"})
    assert not ok
    assert any("executor" in e for e in errs)


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
