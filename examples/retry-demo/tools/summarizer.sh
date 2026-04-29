#!/usr/bin/env bash
# Use Python to build the envelope so embedded newlines / quotes are JSON-safe.
set -euo pipefail
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
patch = inp.get("patch", "")
root_cause = inp.get("root_cause", "")
summary = f"Resolved: {root_cause}. Patch (first 60 chars): {patch[:60].rstrip()}..."
print(json.dumps({
    "status": "success",
    "data": {"summary": summary},
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
