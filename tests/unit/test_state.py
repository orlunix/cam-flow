"""Unit tests for engine.state."""

from camflow.engine.state import apply_updates, init_state


def test_init_state():
    s = init_state()
    assert s == {"pc": "start", "status": "running"}


def test_apply_updates_empty_noop():
    s = {"pc": "start", "status": "running"}
    apply_updates(s, {})
    assert s == {"pc": "start", "status": "running"}


def test_apply_updates_adds_keys():
    s = {"pc": "start"}
    apply_updates(s, {"error": "x"})
    assert s["error"] == "x"


def test_apply_updates_overwrites():
    s = {"pc": "start", "error": "old"}
    apply_updates(s, {"error": "new"})
    assert s["error"] == "new"


def test_apply_updates_none_safe():
    s = {"pc": "start"}
    # Engine may pass None if result had no state_updates key
    apply_updates(s, None)
    assert s == {"pc": "start"}
