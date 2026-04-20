"""Unit tests for planner.scouts.

Both scouts are READ-ONLY and must NEVER raise — every error path
returns a structured warning. These tests assert that contract holds
across: missing tools, malformed output, JSON output, plain-text
output, fallback skill search, env probes for tools that exist + don't,
and path probes.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from camflow.planner import scouts as scouts_mod
from camflow.planner.scouts import (
    DEFAULT_MAX_CANDIDATES,
    default_scout_fn,
    run_env_scout,
    run_skill_scout,
)


# ---- skill-scout: skillm path -------------------------------------------


def _fake_proc(stdout="", stderr="", returncode=0):
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


def test_skill_scout_missing_skillm_uses_fallback(tmp_path):
    """No `skillm` on PATH and no skill dirs → empty candidates, no crash."""
    with patch("camflow.planner.scouts.shutil.which", return_value=None), \
         patch("camflow.planner.scouts._default_skill_dirs", return_value=[]):
        report = run_skill_scout("anything")
    assert report["tool"] == "fallback"
    assert report["candidates"] == []
    assert report["warnings"]  # got at least one warning


def test_skill_scout_skillm_json_output(tmp_path):
    """skillm produces a JSON list — we parse it and read each SKILL.md."""
    skill_dir = tmp_path / "rtl-trace"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: rtl-trace\ndescription: trace RTL signals\n---\n"
        "Body of the skill.\n"
    )
    json_out = json.dumps([
        {"name": "rtl-trace", "path": str(skill_dir / "SKILL.md"),
         "description": "trace RTL signals"},
    ])
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/fake/skillm",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        return_value=_fake_proc(stdout=json_out),
    ):
        report = run_skill_scout("rtl trace")
    assert report["tool"] == "skillm"
    assert len(report["candidates"]) == 1
    c = report["candidates"][0]
    assert c["name"] == "rtl-trace"
    assert "Body of the skill" in c["summary"]


def test_skill_scout_skillm_plain_text(tmp_path):
    """Older skillm: tab-separated lines."""
    skill_md = tmp_path / "x.md"
    skill_md.write_text("---\nname: x\n---\nbody")
    text = f"x\t{skill_md}\tdo X\n"
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/fake/skillm",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        return_value=_fake_proc(stdout=text),
    ):
        report = run_skill_scout("x")
    assert [c["name"] for c in report["candidates"]] == ["x"]


def test_skill_scout_skillm_nonzero_exit(tmp_path):
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/fake/skillm",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        return_value=_fake_proc(returncode=2, stderr="GLIBC error"),
    ):
        report = run_skill_scout("x")
    assert report["candidates"] == []
    assert any("skillm exit 2" in w for w in report["warnings"])


def test_skill_scout_skillm_timeout(tmp_path):
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/fake/skillm",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        side_effect=subprocess.TimeoutExpired("skillm", 30),
    ):
        report = run_skill_scout("x")
    assert any("timed out" in w for w in report["warnings"])


def test_skill_scout_caps_candidates(tmp_path):
    json_out = json.dumps([
        {"name": f"s{i}", "path": "", "description": ""} for i in range(20)
    ])
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/fake/skillm",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        return_value=_fake_proc(stdout=json_out),
    ):
        report = run_skill_scout("x", max_candidates=3)
    assert len(report["candidates"]) == 3


def test_skill_scout_fallback_token_match(tmp_path):
    """Fallback search greps name + description for query tokens."""
    a = tmp_path / "rtl-trace"
    a.mkdir()
    (a / "SKILL.md").write_text(
        "---\nname: rtl-trace\ndescription: trace RTL signals\n---\nbody"
    )
    b = tmp_path / "send-email"
    b.mkdir()
    (b / "SKILL.md").write_text(
        "---\nname: send-email\ndescription: send mail\n---\nbody"
    )
    with patch("camflow.planner.scouts.shutil.which", return_value=None):
        report = run_skill_scout("RTL trace", skill_dirs=[str(tmp_path)])
    names = [c["name"] for c in report["candidates"]]
    assert "rtl-trace" in names
    assert "send-email" not in names


# ---- env-scout ---------------------------------------------------------


def test_env_scout_no_checks_warns():
    report = run_env_scout([])
    assert report["results"] == {}
    assert report["warnings"]


def test_env_scout_tool_present(tmp_path):
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/usr/bin/git",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        return_value=_fake_proc(stdout="git version 2.40.0\n"),
    ):
        report = run_env_scout(["git"])
    info = report["results"]["git"]
    assert info["available"] is True
    assert info["path"] == "/usr/bin/git"
    assert info["version"] == "git version 2.40.0"


def test_env_scout_tool_missing():
    with patch("camflow.planner.scouts.shutil.which", return_value=None):
        report = run_env_scout(["nonexistent-tool"])
    info = report["results"]["nonexistent-tool"]
    assert info["available"] is False
    assert info["path"] is None


def test_env_scout_path_probe(tmp_path):
    f = tmp_path / "thing.txt"
    f.write_text("x")
    report = run_env_scout([
        f"path:{tmp_path}",
        f"path:{f}",
        "path:/no/such/path/here",
    ])
    assert report["results"][f"path:{tmp_path}"]["type"] == "dir"
    assert report["results"][f"path:{f}"]["type"] == "file"
    assert report["results"]["path:/no/such/path/here"]["available"] is False


def test_env_scout_unrecognized_spec():
    report = run_env_scout(["weird spec with spaces"])
    info = report["results"]["weird spec with spaces"]
    assert info["available"] is False
    assert info["kind"] == "unknown"


def test_env_scout_caps_checks():
    report = run_env_scout(["a"] * 50, max_checks=3)
    # Cap applies before deduping → at most 3 distinct keys (here all "a"
    # collapse to one). Either way, cap warning fires.
    assert any("truncated" in w for w in report["warnings"])


def test_env_scout_version_probe_handles_failure():
    """When --version itself errors, tool is still reported as available."""
    with patch(
        "camflow.planner.scouts.shutil.which", return_value="/usr/bin/foo",
    ), patch(
        "camflow.planner.scouts.subprocess.run",
        side_effect=subprocess.TimeoutExpired("foo", 30),
    ):
        report = run_env_scout(["foo"])
    info = report["results"]["foo"]
    assert info["available"] is True
    assert info["version"] is None
    assert info["warning"]  # version probe failure recorded


# ---- default_scout_fn dispatcher ---------------------------------------


def test_default_scout_fn_skill():
    with patch(
        "camflow.planner.scouts.run_skill_scout",
        return_value={"query": "x", "candidates": [], "warnings": []},
    ) as run:
        out = default_scout_fn("skill", "x")
    run.assert_called_once_with("x")
    assert "candidates" in out


def test_default_scout_fn_env_string_to_list():
    with patch(
        "camflow.planner.scouts.run_env_scout",
        return_value={"results": {}, "warnings": []},
    ) as run:
        default_scout_fn("env", "vcs")
    # String query is wrapped into a single-element list.
    assert run.call_args.args[0] == ["vcs"]


def test_default_scout_fn_unknown_type():
    out = default_scout_fn("bogus", "x")
    assert any("unknown scout_type" in w for w in out["warnings"])
