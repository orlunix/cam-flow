def choose_recovery_action(state, error=None):
    """Return a simple recovery decision based on current state and error."""
    retry = state.get("retry", 0)

    if retry >= 2:
        return {
            "action": "reroute",
            "target": state.get("recovery_node", "done"),
            "reason": "retry budget exhausted"
        }

    return {
        "action": "retry",
        "target": state.get("pc"),
        "reason": "retry allowed"
    }
