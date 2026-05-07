from types import SimpleNamespace

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
    assert kwargs["timeout"] == 30


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
