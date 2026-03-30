import time

TIMEOUT_SEC = 300


def check_timeout(last_progress_ts):
    if not last_progress_ts:
        return False

    return (time.time() - last_progress_ts) > TIMEOUT_SEC


def handle_timeout(state):
    state["status"] = "failed"
    return state


def supervisor_step(state, last_progress_ts):
    if check_timeout(last_progress_ts):
        return handle_timeout(state)

    return state
