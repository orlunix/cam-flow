#!/usr/bin/env bash
exec python3 -c '
import json, sys
json.load(sys.stdin)
print(json.dumps({
    "status": "success",
    "data": {
        "warnings_found": 1,
        "notes": "Trailing whitespace on line 12.",
    },
    "error": None,
    "metrics": {"checks_run": 23},
    "artifacts": [],
}))
'
