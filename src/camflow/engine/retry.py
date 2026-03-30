"""Retry policy.

Implements: spec/retry.md
"""

MAX_RETRY = 2


def should_retry(state, result):
    if result.get("status") != "fail":
        return False
    return state.get("retry", 0) < MAX_RETRY


def apply_retry(state):
    state["retry"] = state.get("retry", 0) + 1
    return state
