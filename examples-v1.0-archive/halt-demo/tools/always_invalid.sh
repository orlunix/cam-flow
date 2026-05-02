#!/usr/bin/env bash
# This validator always returns ok=false. Combined with retry:
# until: ok==true and max_attempts: 2, the workflow exhausts retries and halts.
exec python3 -c '
import json, sys, os
inp = json.load(sys.stdin)
attempt = os.environ.get("CAMFLOW_ATTEMPT", "?")
print(json.dumps({
    "status": "success",
    "data": {
        "ok": False,
        "reason": "attempt {}: schema mismatch on field x".format(attempt),
    },
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
