#!/usr/bin/env bash
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
print(json.dumps({
    "status": "success",
    "data": {"artifact": "doc-for-{}".format(inp.get("request_id", "?"))},
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
