"""Unit tests for backend.cam.cmd_runner."""

from camflow.backend.cam.cmd_runner import run_cmd


def test_success(tmp_path):
    r = run_cmd("echo hello", str(tmp_path))
    assert r["status"] == "success"
    assert r["output"]["exit_code"] == 0
    assert "hello" in r["output"]["stdout_tail"]
    assert "hello" in r["state_updates"]["last_cmd_output"]
    assert r["error"] is None


def test_failure(tmp_path):
    r = run_cmd("false", str(tmp_path))
    assert r["status"] == "fail"
    assert r["output"]["exit_code"] == 1
    assert r["error"]["code"] == "CMD_FAIL"


def test_stderr_captured(tmp_path):
    r = run_cmd("echo boom >&2; exit 1", str(tmp_path))
    assert r["status"] == "fail"
    assert "boom" in r["output"]["stderr_tail"]
    assert "boom" in r["state_updates"]["last_cmd_stderr"]


def test_stdout_truncation(tmp_path):
    # Print 5000 chars; tail is capped at 2000
    r = run_cmd('python3 -c "print(\'x\'*5000)"', str(tmp_path))
    assert r["status"] == "success"
    assert len(r["output"]["stdout_tail"]) == 2000
    assert len(r["state_updates"]["last_cmd_output"]) == 2000


def test_timeout(tmp_path):
    r = run_cmd("sleep 10", str(tmp_path), timeout=1)
    assert r["status"] == "fail"
    assert r["error"]["code"] == "CMD_TIMEOUT"
