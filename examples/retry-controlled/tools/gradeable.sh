#!/usr/bin/env bash
# Deterministic grader: pass on attempt >= pass_at, otherwise fail with feedback.
# CAMFLOW_ATTEMPT (1-indexed) is set by the runner.
exec python3 -c '
import json, sys, os
inp = json.load(sys.stdin)
attempt = int(os.environ.get("CAMFLOW_ATTEMPT", "1"))
pass_at = int(inp.get("pass_at", 1))

ok = attempt >= pass_at
if ok:
    reason = "pass_at threshold reached at attempt {}".format(attempt)
else:
    reason = (
        "attempt {} < pass_at {}; not yet — runner should retry. "
        "Use this feedback as guidance for the next attempt."
        .format(attempt, pass_at)
    )

print(json.dumps({
    "status": "success",
    "data": {
        "value": attempt * 10,
        "ok": ok,
        "reason": reason,
    },
    "error": None,
    "metrics": {"attempt": attempt, "pass_at": pass_at},
    "artifacts": [],
}))
'
