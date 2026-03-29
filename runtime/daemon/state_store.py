def init_state():
    return {
        "pc": "start",
        "status": "running"
    }


def apply_updates(state, updates):
    if not updates:
        return state

    state.update(updates)
    return state
