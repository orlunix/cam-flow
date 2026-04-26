"""Verify that ``camflow plan`` routes between the agent Planner
(default) and the legacy single-shot Planner (``--legacy``).

Both branches are otherwise covered by ``test_agent_planner.py`` and
``test_planner.py``; here we only assert the dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from camflow.cli_entry import plan as plan_module


def _args(**kwargs):
    """Minimal argparse-Namespace-shaped object with the fields
    plan_command reads. Defaults match the parser's defaults."""
    import argparse
    base = {
        "request": "build a thing",
        "claude_md": None,
        "skills_dir": None,
        "output": None,
        "force": False,
        "domain": None,
        "agents_dir": None,
        "scout_report": [],
        "legacy": False,
        "project_dir": None,
        "timeout": 30,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestDispatch:
    def test_default_uses_agent_planner(self, tmp_path, monkeypatch):
        """Default (no --legacy) should call the agent path."""
        called: dict = {"agent": False, "legacy": False}

        def fake_agent(args):
            called["agent"] = True
            return 0

        def fake_legacy(args):
            called["legacy"] = True
            return 0

        monkeypatch.setattr(plan_module, "_plan_via_agent", fake_agent)
        monkeypatch.setattr(plan_module, "_plan_legacy", fake_legacy)

        rc = plan_module.plan_command(_args(project_dir=str(tmp_path)))
        assert rc == 0
        assert called["agent"] is True
        assert called["legacy"] is False

    def test_legacy_flag_routes_to_legacy(self, tmp_path, monkeypatch):
        called: dict = {"agent": False, "legacy": False}

        def fake_agent(args):
            called["agent"] = True
            return 0

        def fake_legacy(args):
            called["legacy"] = True
            return 0

        monkeypatch.setattr(plan_module, "_plan_via_agent", fake_agent)
        monkeypatch.setattr(plan_module, "_plan_legacy", fake_legacy)

        rc = plan_module.plan_command(
            _args(project_dir=str(tmp_path), legacy=True)
        )
        assert rc == 0
        assert called["legacy"] is True
        assert called["agent"] is False


class TestAgentBranchSurfacesError:
    def test_planner_agent_error_returns_1_with_legacy_hint(
        self, tmp_path, monkeypatch, capsys,
    ):
        from camflow.planner.agent_planner import PlannerAgentError

        def fake_generate(*a, **kw):
            raise PlannerAgentError("camc unreachable")

        monkeypatch.setattr(
            plan_module, "generate_workflow_via_agent", fake_generate,
        )

        rc = plan_module._plan_via_agent(_args(project_dir=str(tmp_path)))
        err = capsys.readouterr().err
        assert rc == 1
        assert "Planner agent failed" in err
        assert "--legacy" in err


class TestArgparseHookup:
    def test_parser_accepts_new_flags(self):
        parser = plan_module.build_parser(None)
        ns = parser.parse_args(
            ["build", "--legacy", "--project-dir", "/tmp/x", "--timeout", "60"]
        )
        assert ns.legacy is True
        assert ns.project_dir == "/tmp/x"
        assert ns.timeout == 60
