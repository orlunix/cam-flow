"""State management.

Implements: spec/state.md
"""


def init_state(first_node="start"):
    """Initialize a fresh workflow state.

    `first_node` is the node id that pc should start at. Defaults to
    "start" for back-compat, but the engine passes the first node it
    finds in the loaded workflow so users aren't required to name the
    entry node "start".
    """
    return {
        "pc": first_node,
        "status": "running"
    }


def apply_updates(state, updates):
    if not updates:
        return state
    state.update(updates)
    return state
