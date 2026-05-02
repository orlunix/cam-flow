#!/usr/bin/env bash
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
print(json.dumps({
    "status": "success",
    "data": {
        "summary": "PR #{}: {}".format(inp["pr"], inp["title"]),
        "lines_changed": 14,
    },
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
