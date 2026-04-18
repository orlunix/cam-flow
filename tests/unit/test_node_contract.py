"""Unit tests for engine.node_contract.validate_result."""

from camflow.engine.node_contract import validate_result


def _full():
    return {
        "status": "success",
        "summary": "ok",
        "output": {},
        "state_updates": {},
        "control": {"action": None, "target": None, "reason": None},
        "error": None,
    }


def test_valid_result():
    ok, err = validate_result(_full())
    assert ok, err


def test_not_a_dict():
    ok, err = validate_result("not a dict")
    assert not ok
    assert "not a dict" in err


def test_missing_key():
    r = _full()
    del r["summary"]
    ok, err = validate_result(r)
    assert not ok
    assert "summary" in err


def test_invalid_status():
    r = _full()
    r["status"] = "ok"
    ok, err = validate_result(r)
    assert not ok
    assert "invalid status" in err


def test_control_not_dict():
    r = _full()
    r["control"] = "bad"
    ok, err = validate_result(r)
    assert not ok
    assert "control" in err


def test_invalid_control_action():
    r = _full()
    r["control"]["action"] = "explode"
    ok, err = validate_result(r)
    assert not ok
    assert "action" in err


def test_state_updates_not_dict():
    r = _full()
    r["state_updates"] = []
    ok, err = validate_result(r)
    assert not ok


def test_output_not_dict():
    r = _full()
    r["output"] = "x"
    ok, err = validate_result(r)
    assert not ok
