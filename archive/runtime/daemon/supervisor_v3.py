from runtime.daemon.recovery_policy import choose_recovery_action


def supervisor_step(state, error=None):
    if state.get("status") == "failed":
        decision = choose_recovery_action(state, error)

        if decision["action"] == "retry":
            state["status"] = "running"
            return state

        if decision["action"] == "reroute":
            state["pc"] = decision["target"]
            state["status"] = "running"
            return state

    return state
