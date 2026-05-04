# `parse_record` specification

`lib.csvparser.parse_record(line: str) -> list[str]` parses a single
CSV-format line into its fields.

This module powers a small CSV import path. Callers expect the
behaviors below — **all required.** The default visible test suite
covers only Req 1; **see also `tests/invariants/` for the rest of
the deterministic checks.**

## Req 1 — Comma-separated splitting

A line of comma-separated values is split into N fields:

    parse_record("a,b,c") == ["a", "b", "c"]

Exercised by `tests/test_csvparser.py::test_basic_split`.

## Req 2 — Surrounding whitespace stripped per field

Whitespace before and after each field is removed. Whitespace
*inside* a field (e.g. `"foo bar"` containing a single space) is
preserved.

    parse_record(" a , b ,  c") == ["a", "b", "c"]

Exercised by `tests/invariants/test_invariants.py::test_strip_surrounding_whitespace`.

## Req 3 — Quoted fields contain inner commas

A double-quoted field `"..."` is one field; commas inside the quotes
are NOT separators. The surrounding quotes are removed; whitespace
inside the quotes is preserved verbatim.

    parse_record('a,"b, c",d') == ["a", "b, c", "d"]

Exercised by `tests/invariants/test_invariants.py::test_quoted_field_with_comma`.

## Req 4 — Doubled quote inside a quoted field

Inside a quoted field, the sequence `""` (two double quotes adjacent)
is the literal character `"`. This is the standard CSV escaping rule
and it's easy to miss if you implement quote handling without
thinking through escapes.

    parse_record('"hello ""world"""') == ['hello "world"']

Exercised by `tests/invariants/test_invariants.py::test_doubled_quote_inside_quoted_field`.
