#!/usr/bin/env bash
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
print(json.dumps({
    "status": "success",
    "data": {
        "result": "PR #{} blocked. Reason: {}".format(inp["pr"], inp["reasoning"]),
    },
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
