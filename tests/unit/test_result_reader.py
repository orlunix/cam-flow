"""Unit tests for backend.cam.result_reader."""

import json
import os

from camflow.backend.cam.result_reader import (
    clear_node_result,
    read_node_result,
)


def _write_result(project_dir, content):
    os.makedirs(os.path.join(project_dir, ".camflow"), exist_ok=True)
    path = os.path.join(project_dir, ".camflow", "node-result.json")
    if isinstance(content, (dict, list)):
        with open(path, "w") as f:
            json.dump(content, f)
    else:
        with open(path, "w") as f:
            f.write(content)
    return path


def test_missing_file(tmp_path):
    r = read_node_result(str(tmp_path))
    assert r["status"] == "fail"
    assert "did not write" in r["summary"]


def test_valid_result(tmp_path):
    _write_result(str(tmp_path), {
        "status": "success",
        "summary": "done",
        "state_updates": {"x": 1},
    })
    r = read_node_result(str(tmp_path))
    assert r["status"] == "success"
    assert r["state_updates"] == {"x": 1}
    assert r["error"] is None  # default


def test_malformed_json(tmp_path):
    _write_result(str(tmp_path), "{this is not json")
    r = read_node_result(str(tmp_path))
    assert r["status"] == "fail"
    assert "malformed" in r["summary"]


def test_not_a_dict(tmp_path):
    _write_result(str(tmp_path), [1, 2, 3])
    r = read_node_result(str(tmp_path))
    assert r["status"] == "fail"
    assert "JSON object" in r["summary"]


def test_missing_required_keys(tmp_path):
    _write_result(str(tmp_path), {"status": "success"})  # no summary
    r = read_node_result(str(tmp_path))
    assert r["status"] == "fail"
    assert "missing" in r["summary"]


def test_clear_removes_file(tmp_path):
    _write_result(str(tmp_path), {"status": "success", "summary": "x"})
    clear_node_result(str(tmp_path))
    assert not os.path.exists(os.path.join(str(tmp_path), ".camflow", "node-result.json"))


def test_clear_when_missing_is_noop(tmp_path):
    clear_node_result(str(tmp_path))  # should not raise
