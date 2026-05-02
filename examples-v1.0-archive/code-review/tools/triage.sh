#!/usr/bin/env bash
exec python3 -c '
import json, sys
inp = json.load(sys.stdin)
sev = inp["security_severity"]
issues = int(inp["security_issues"])
warns = int(inp["style_warnings"])

approved = (sev != "high" and issues == 0 and warns <= 5)
reasoning = (
    f"security={sev}/{issues}, style_warnings={warns} → "
    + ("approve" if approved else "block")
)
print(json.dumps({
    "status": "success",
    "data": {"approved": approved, "reasoning": reasoning},
    "error": None,
    "metrics": {},
    "artifacts": [],
}))
'
