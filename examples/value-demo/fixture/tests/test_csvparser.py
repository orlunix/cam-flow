"""Visible tests — only Req 1 is exercised here.

See also tests/invariants/ for Req 2-4.
"""
from lib.csvparser import parse_record


def test_basic_split():
    """Req 1: a line of comma-separated values is split into N fields."""
    assert parse_record("a,b,c") == ["a", "b", "c"]
