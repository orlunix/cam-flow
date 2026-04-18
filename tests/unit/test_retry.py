"""Unit tests for engine.retry."""

from camflow.engine.retry import MAX_RETRY, apply_retry, should_retry


def test_success_never_retried():
    assert not should_retry({}, {"status": "success"})


def test_fail_retried_within_budget():
    assert should_retry({"retry": 0}, {"status": "fail"})
    assert should_retry({"retry": 1}, {"status": "fail"})


def test_fail_not_retried_at_budget():
    assert not should_retry({"retry": MAX_RETRY}, {"status": "fail"})


def test_apply_retry_increments():
    s = {}
    apply_retry(s)
    assert s["retry"] == 1
    apply_retry(s)
    assert s["retry"] == 2
