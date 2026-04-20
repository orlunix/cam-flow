"""Unit tests for the `camflow scout` CLI subcommand.

The CLI is a thin shell over planner.scouts. We validate:
  * argparse wiring (required flags, choices)
  * JSON output is valid and round-trips through json.loads
  * skill / env type both produce the expected report shape
  * --pretty toggles indented JSON
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from camflow.cli_entry.scout import build_parser, scout_command


def _run(argv):
    """Invoke the CLI like main() would, returning (exit_code, stdout)."""
    parser = build_parser(None)
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    return rc, buf.getvalue()


def test_skill_scout_emits_json():
    fake = {"query": "x", "tool": "skillm", "candidates": [
        {"name": "rtl-trace", "description": "trace RTL", "summary": "...",
         "path": "/x"},
    ], "warnings": []}
    with patch(
        "camflow.cli_entry.scout.run_skill_scout", return_value=fake,
    ) as run:
        rc, out = _run(["--type", "skill", "--query", "trace RTL"])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed == fake
    # Compact (no leading whitespace) by default.
    assert "\n  " not in out
    run.assert_called_once()


def test_skill_scout_pretty_indents():
    fake = {"query": "x", "tool": "skillm", "candidates": [], "warnings": []}
    with patch("camflow.cli_entry.scout.run_skill_scout", return_value=fake):
        rc, out = _run(["--type", "skill", "--query", "x", "--pretty"])
    assert rc == 0
    assert "\n  " in out  # indented
    json.loads(out)       # still valid


def test_env_scout_multi_query():
    fake = {"checks": ["vcs", "smake"], "results": {
        "vcs": {"kind": "tool", "available": True, "path": "/x", "version": "v"},
        "smake": {"kind": "tool", "available": False, "path": None,
                  "version": None, "warning": "not on PATH"},
    }, "warnings": []}
    with patch(
        "camflow.cli_entry.scout.run_env_scout", return_value=fake,
    ) as run:
        rc, out = _run([
            "--type", "env", "--query", "vcs", "--query", "smake",
        ])
    assert rc == 0
    parsed = json.loads(out)
    assert set(parsed["results"].keys()) == {"vcs", "smake"}
    # Caller passed the list of checks unchanged.
    assert run.call_args.args[0] == ["vcs", "smake"]


def test_skill_scout_requires_query():
    rc, _ = _run(["--type", "skill"])
    assert rc == 2


def test_env_scout_requires_query():
    rc, _ = _run(["--type", "env"])
    assert rc == 2


def test_unknown_type_rejected_by_argparse():
    with pytest.raises(SystemExit):
        build_parser(None).parse_args(["--type", "unknown", "--query", "x"])
