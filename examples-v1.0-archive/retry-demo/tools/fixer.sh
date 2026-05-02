#!/usr/bin/env bash
# Reads input.feedback (if any) from stdin, returns a patch annotated with
# attempt number and any feedback received from the previous round.
set -euo pipefail
input=$(cat)
attempt="${CAMFLOW_ATTEMPT:-1}"
feedback=$(printf '%s' "$input" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('feedback','') or '')
except Exception:
    print('')
")

if [ -n "$feedback" ]; then
  explanation="attempt $attempt: addressing prior feedback ($feedback)"
else
  explanation="attempt $attempt: initial fix — added null check"
fi

cat <<EOF
{
  "status": "success",
  "data": {
    "patch": "diff --git a/parser.py b/parser.py\\n+    if input is None: return []\\n",
    "explanation": "$explanation"
  },
  "error": null,
  "metrics": {},
  "artifacts": []
}
EOF
