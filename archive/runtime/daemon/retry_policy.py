MAX_RETRY = 2


def should_retry(state, result):
    if result.get("status") != "fail":
        return False

    retry = state.get("retry", 0)
    return retry < MAX_RETRY


def apply_retry(state):
    state["retry"] = state.get("retry", 0) + 1
    return state
