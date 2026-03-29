from runtime.daemon.persistence import load_state


def resume_state(state_path):
    state = load_state(state_path)
    if not state:
        return None

    return state
