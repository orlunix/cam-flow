from types import SimpleNamespace
import errno

import pytest

from runner import camc_lib


def test_spawn_uses_camc_default_tool(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Starting claude agent abc12345\n",
            stderr="",
        )

    monkeypatch.delenv("CAMFLOW_AGENT_TOOL", raising=False)
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn("prompt", tmp_path, "node-attempt-1", "tag") == "abc12345"

    cmd, kwargs = calls[0]
    assert cmd[:2] == ["camc", "run"]
    assert "--tool" not in cmd
    assert cmd[-1] == "prompt"
    assert kwargs["timeout"] == 180


def test_spawn_uses_prompt_file_for_large_prompt(monkeypatch, tmp_path):
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Starting claude agent abc12345\n",
            stderr="",
        )

    prompt = "x" * (camc_lib.DEFAULT_CAMC_PROMPT_ARG_MAX_BYTES + 1)
    monkeypatch.delenv("CAMFLOW_AGENT_TOOL", raising=False)
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)
    monkeypatch.setattr(camc_lib.time, "sleep", lambda n: sleeps.append(n))

    assert camc_lib.spawn(prompt, tmp_path, "node-attempt-1", "tag") == "abc12345"

    cmd, _kwargs = calls[0]
    assert cmd[-1] != prompt
    assert ".camflow_camc_prompt.txt" in cmd[-1]
    assert (tmp_path / ".camflow_camc_prompt.txt").read_text() == prompt
    assert sleeps == [camc_lib.DEFAULT_CAMC_PROMPT_FILE_SUBMIT_DELAY_S]
    assert calls[1][0] == ["camc", "key", "abc12345", "Enter"]


def test_spawn_retries_with_prompt_file_on_e2big(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            raise OSError(errno.E2BIG, "Argument list too long", "camc")
        return SimpleNamespace(
            returncode=0,
            stdout="Starting claude agent abc12345\n",
            stderr="",
        )

    monkeypatch.setenv("CAMFLOW_CAMC_PROMPT_ARG_MAX_BYTES", "0")
    monkeypatch.setenv("CAMFLOW_CAMC_PROMPT_FILE_SUBMIT_DELAY", "0")
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn("short prompt", tmp_path,
                          "node-attempt-1", "tag") == "abc12345"

    assert calls[0][-1] == "short prompt"
    assert ".camflow_camc_prompt.txt" in calls[1][-1]
    assert calls[2] == ["camc", "key", "abc12345", "Enter"]
    assert (tmp_path / ".camflow_camc_prompt.txt").read_text() == "short prompt"


def test_spawn_nudges_prompt_file_agent_after_timeout_recovery(monkeypatch,
                                                               tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["camc", "run"]:
            raise camc_lib.subprocess.TimeoutExpired(
                cmd,
                kwargs["timeout"],
                output="Starting claude agent facefeed\n",
            )
        return SimpleNamespace(returncode=0, stdout="Sent key: Enter\n", stderr="")

    prompt = "x" * (camc_lib.DEFAULT_CAMC_PROMPT_ARG_MAX_BYTES + 1)
    monkeypatch.setenv("CAMFLOW_CAMC_PROMPT_FILE_SUBMIT_DELAY", "0")
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn(prompt, tmp_path, "node-attempt-1", "tag") == "facefeed"
    assert ".camflow_camc_prompt.txt" in calls[0][-1]
    assert calls[1] == ["camc", "key", "facefeed", "Enter"]


def test_spawn_honors_camflow_agent_tool(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="Starting codex agent def67890\n",
            stderr="",
        )

    monkeypatch.setenv("CAMFLOW_AGENT_TOOL", "codex")
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn("prompt", tmp_path, "node-attempt-1", "tag") == "def67890"

    cmd = calls[0]
    assert cmd[:4] == ["camc", "run", "--tool", "codex"]
    assert "--path" in cmd
    assert "--name" in cmd
    assert "--tag" in cmd


def test_spawn_honors_camflow_camc_run_timeout(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="Starting claude agent abc99999\n",
            stderr="",
        )

    monkeypatch.setenv("CAMFLOW_CAMC_RUN_TIMEOUT", "240")
    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn("prompt", tmp_path, "node-attempt-1", "tag") == "abc99999"
    assert calls[0]["timeout"] == 240


def test_spawn_timeout_can_recover_agent_id_from_partial_stdout(monkeypatch,
                                                               tmp_path):
    def fake_run(cmd, **kwargs):
        raise camc_lib.subprocess.TimeoutExpired(
            cmd,
            kwargs["timeout"],
            output="Starting claude agent deadbeef\n",
        )

    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    assert camc_lib.spawn("prompt", tmp_path, "node-attempt-1", "tag") == "deadbeef"


def test_spawn_timeout_without_agent_id_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise camc_lib.subprocess.TimeoutExpired(
            cmd,
            kwargs["timeout"],
            output="initializing...\n",
        )

    monkeypatch.setattr(camc_lib.subprocess, "run", fake_run)

    with pytest.raises(camc_lib.CamcTimeout):
        camc_lib.spawn("prompt", tmp_path, "node-attempt-1", "tag")
