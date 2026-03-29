REQUIRED_KEYS = ["status", "summary", "output", "state_updates", "control", "error"]
VALID_STATUS = {"success", "fail", "wait", "abort"}
VALID_ACTIONS = {"continue", "goto", "wait", "fail", "abort", None}


def validate_result(result):
    if not isinstance(result, dict):
        return False, "result is not a dict"

    for key in REQUIRED_KEYS:
        if key not in result:
            return False, f"missing key: {key}"

    status = result.get("status")
    if status not in VALID_STATUS:
        return False, f"invalid status: {status}"

    control = result.get("control") or {}
    if not isinstance(control, dict):
        return False, "control must be a dict"

    action = control.get("action")
    if action not in VALID_ACTIONS:
        return False, f"invalid control.action: {action}"

    if not isinstance(result.get("state_updates"), dict):
        return False, "state_updates must be a dict"

    if not isinstance(result.get("output"), dict):
        return False, "output must be a dict"

    return True, None
