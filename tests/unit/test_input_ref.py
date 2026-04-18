"""Unit tests for engine.input_ref.resolve_refs."""

from camflow.engine.input_ref import resolve_refs


def test_empty_text():
    assert resolve_refs("", {"a": 1}) == ""


def test_none_text():
    assert resolve_refs(None, {"a": 1}) == ""


def test_substitution():
    assert resolve_refs("error: {{state.error}}", {"error": "oops"}) == "error: oops"


def test_no_match_leaves_placeholder():
    # If the state key doesn't exist, the placeholder remains (explicit, caller can detect)
    text = "x: {{state.missing}}"
    assert resolve_refs(text, {"other": 1}) == text


def test_multiple_substitutions():
    out = resolve_refs("{{state.a}}/{{state.b}}", {"a": "A", "b": "B"})
    assert out == "A/B"


def test_non_string_values_coerced():
    assert resolve_refs("n={{state.count}}", {"count": 42}) == "n=42"


def test_list_value_str_repr():
    out = resolve_refs("ls={{state.ls}}", {"ls": [1, 2]})
    assert "1" in out and "2" in out
