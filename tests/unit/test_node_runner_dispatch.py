"""Unit tests for the standalone backend.cam.node_runner dispatcher.

Covers DSL v2 dispatch: shell/cmd alias, agent <name>, skill <name>,
inline prompt, and the invalid-do fallback. Agent-producing branches
are validated by asserting on the prompt passed into run_agent
(patched), rather than actually spawning camc.
"""

from unittest.mock import patch

import pytest

from camflow.backend.cam import agent_loader
from camflow.backend.cam.agent_loader import AGENTS_DIR_ENV
from camflow.backend.cam.node_runner import run_node


def _fake_agent_result(status="success"):
    return (
        {"status": status, "summary": "ok", "state_updates": {}, "error": None},
        "agent-abc",
        "file_appeared",
    )


def test_dispatch_shell_runs_cmd(tmp_path):
    node = {"do": "shell echo hi"}
    with patch("camflow.backend.cam.node_runner.run_cmd") as run_cmd:
        run_cmd.return_value = {"status": "success"}
        run_node("n", node, {}, str(tmp_path))
    run_cmd.assert_called_once()
    assert run_cmd.call_args[0][0] == "echo hi"


def test_dispatch_cmd_alias(tmp_path):
    """`cmd` still works as an alias for `shell`."""
    node = {"do": "cmd echo legacy"}
    with patch("camflow.backend.cam.node_runner.run_cmd") as run_cmd:
        run_cmd.return_value = {"status": "success"}
        run_node("n", node, {}, str(tmp_path))
    run_cmd.assert_called_once()
    assert run_cmd.call_args[0][0] == "echo legacy"


def test_dispatch_agent_claude_tries_loader(tmp_path, monkeypatch):
    """`agent claude` is NOT special-cased any more. The DSL treats
    the name "claude" like any other: the loader looks for
    ~/.claude/agents/claude.md and, if absent, returns None so the
    node runs anonymously. New workflows should use inline prompts
    (`do: "<task>"`) instead of `agent claude`.
    """
    # Pin CAMFLOW_AGENTS_DIR to an empty tmp dir so the test doesn't
    # accidentally pick up a real `claude.md` on the host.
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    node = {"do": "agent claude", "with": "do stuff"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ) as run_agent, patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    build_prompt.assert_called_once()
    # No ~/.claude/agents/claude.md in the pinned dir → agent_def None.
    assert build_prompt.call_args.kwargs.get("agent_def") is None
    run_agent.assert_called_once()


def test_dispatch_agent_claude_resolves_when_file_exists(tmp_path, monkeypatch):
    """If someone DID write ~/.claude/agents/claude.md, it's treated
    like any other agent — nothing about the name "claude" is magic.
    """
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    (tmp_path / "claude.md").write_text(
        "---\nname: claude\ndescription: custom generic agent\n---\n"
        "You are a specialized variant of the default agent.\n"
    )
    node = {"do": "agent claude", "with": "do stuff"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ), patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    agent_def = build_prompt.call_args.kwargs["agent_def"]
    assert agent_def is not None
    assert agent_def["name"] == "claude"


def test_dispatch_agent_named_loads_definition(tmp_path, monkeypatch):
    """`agent rtl-debugger` → loads ~/.claude/agents/rtl-debugger.md."""
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    (tmp_path / "rtl-debugger.md").write_text(
        "---\nname: rtl-debugger\ndescription: test\n"
        "tools: [Read, Bash]\n---\nYou are an RTL debugger.\n"
    )
    node = {"do": "agent rtl-debugger", "with": "fix it"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ), patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    agent_def = build_prompt.call_args.kwargs["agent_def"]
    assert agent_def is not None
    assert agent_def["name"] == "rtl-debugger"
    assert agent_def["tools"] == ["Read", "Bash"]


def test_dispatch_agent_missing_definition_is_anonymous(tmp_path, monkeypatch):
    """Reference to an unknown agent falls through to anonymous (no crash)."""
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    node = {"do": "agent ghost", "with": "x"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ), patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    assert build_prompt.call_args.kwargs.get("agent_def") is None


def test_dispatch_skill_sets_inline_task(tmp_path):
    node = {"do": "skill rtl-trace", "with": "trace ECC"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ), patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    inline = build_prompt.call_args.kwargs["inline_task"]
    assert "Invoke the skill named 'rtl-trace'" in inline
    assert "trace ECC" in inline


def test_dispatch_inline_prompt(tmp_path):
    """Free text in `do` becomes the inline task — no `with` needed."""
    node = {"do": "Fix the bug in calculator.py"}
    with patch(
        "camflow.backend.cam.node_runner.run_agent",
        return_value=_fake_agent_result(),
    ), patch(
        "camflow.backend.cam.node_runner.build_prompt",
        return_value="<prompt>",
    ) as build_prompt:
        run_node("n", node, {}, str(tmp_path))
    assert build_prompt.call_args.kwargs["inline_task"] == (
        "Fix the bug in calculator.py"
    )
    assert build_prompt.call_args.kwargs.get("agent_def") is None


def test_dispatch_invalid_do(tmp_path):
    """Empty `do` produces an INVALID_DO error result."""
    r = run_node("n", {"do": "   "}, {}, str(tmp_path))
    assert r["status"] == "fail"
    assert r["error"]["code"] == "INVALID_DO"
