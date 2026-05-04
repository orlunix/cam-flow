"""Invariant tests — Req 2-4 of SPEC.md.

These are referenced by SPEC.md ("see also `tests/invariants/`") but
are NOT collected by the default `pytest tests/` invocation
(`run_default_tests.sh` adds `--ignore=tests/invariants`). Run them
separately via `pytest tests/invariants/`.
"""
from lib.csvparser import parse_record


def test_strip_surrounding_whitespace():
    """Req 2: surrounding whitespace on each field is stripped;
    whitespace inside a field stays."""
    assert parse_record(" a , b ,  c") == ["a", "b", "c"]


def test_quoted_field_with_comma():
    """Req 3: a double-quoted field is one field; inner commas are
    NOT separators; surrounding quotes are removed."""
    assert parse_record('a,"b, c",d') == ["a", "b, c", "d"]


def test_doubled_quote_inside_quoted_field():
    """Req 4: inside a quoted field, "" is a literal "."""
    assert parse_record('"hello ""world"""') == ['hello "world"']
