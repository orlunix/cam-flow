#!/usr/bin/env bash
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
v = inp.get("result_value", "?")
print(json.dumps({
    "status": "success",
    "data": {"summary": "finalized with value=" + str(v)},
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
