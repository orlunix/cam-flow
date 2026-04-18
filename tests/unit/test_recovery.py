"""Unit tests for engine.recovery."""

from camflow.engine.recovery import choose_recovery_action


def test_retry_when_under_budget():
    d = choose_recovery_action({"retry": 1, "pc": "fix"})
    assert d["action"] == "retry"
    assert d["target"] == "fix"


def test_reroute_when_exhausted():
    d = choose_recovery_action({"retry": 2, "recovery_node": "rescue"})
    assert d["action"] == "reroute"
    assert d["target"] == "rescue"


def test_reroute_default_target():
    d = choose_recovery_action({"retry": 5})
    assert d["target"] == "done"
