#!/usr/bin/env bash
exec python3 -c '
import json, sys
json.load(sys.stdin)  # input ignored in this stub
print(json.dumps({
    "status": "success",
    "data": {
        "severity": "low",
        "issues_found": 0,
        "notes": "No security issues detected. Change is a defensive null check.",
    },
    "error": None,
    "metrics": {"scan_duration_ms": 47},
    "artifacts": [],
}))
'
