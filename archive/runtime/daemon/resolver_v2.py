def resolve_next(node_id, node, result, state):
    """Resolve the next workflow step.

    Priority:
    1. control.action = abort
    2. control.action = wait
    3. if fail
    4. other if conditions
    5. explicit next
    6. default done/failed
    """

    control = result.get("control", {}) or {}
    action = control.get("action")
    target = control.get("target")

    if action == "abort":
        return {
            "workflow_status": "aborted",
            "next_pc": None,
            "resume_pc": None,
            "reason": "control.action=abort"
        }

    if action == "wait":
        return {
            "workflow_status": "waiting",
            "next_pc": node_id,
            "resume_pc": target or node_id,
            "reason": "control.action=wait"
        }

    transitions = node.get("transitions", []) or []

    # if fail shortcut
    if result.get("status") == "fail":
        for rule in transitions:
            if rule.get("if") == "fail":
                return {
                    "workflow_status": "running",
                    "next_pc": rule["goto"],
                    "resume_pc": None,
                    "reason": "matched if fail"
                }

    # simple conditions
    for rule in transitions:
        cond = rule.get("if", "")
        if cond.startswith("output."):
            key = cond.split(".", 1)[1]
            if result.get("output", {}).get(key):
                return {
                    "workflow_status": "running",
                    "next_pc": rule["goto"],
                    "resume_pc": None,
                    "reason": f"matched {cond}"
                }
        elif cond.startswith("state."):
            key = cond.split(".", 1)[1]
            if state.get(key):
                return {
                    "workflow_status": "running",
                    "next_pc": rule["goto"],
                    "resume_pc": None,
                    "reason": f"matched {cond}"
                }

    if action == "goto" and target:
        return {
            "workflow_status": "running",
            "next_pc": target,
            "resume_pc": None,
            "reason": "control.action=goto"
        }

    if "next" in node:
        return {
            "workflow_status": "running",
            "next_pc": node["next"],
            "resume_pc": None,
            "reason": "matched next"
        }

    if result.get("status") == "fail":
        return {
            "workflow_status": "failed",
            "next_pc": None,
            "resume_pc": None,
            "reason": "unhandled node failure"
        }

    return {
        "workflow_status": "done",
        "next_pc": None,
        "resume_pc": None,
        "reason": "no next transition"
    }
