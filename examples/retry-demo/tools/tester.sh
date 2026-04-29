#!/usr/bin/env bash
# Flaky tester: fails attempts 1 and 2 with feedback, passes attempt 3+.
# This drives the retry loop in the demo workflow.
set -euo pipefail
cat > /dev/null
attempt="${CAMFLOW_ATTEMPT:-1}"

if [ "$attempt" -lt 3 ]; then
  cat <<EOF
{
  "status": "success",
  "data": {
    "passed": false,
    "feedback": "attempt $attempt failed: edge case 'empty list' still throws"
  },
  "error": null,
  "metrics": {"failed_tests": 1},
  "artifacts": []
}
EOF
else
  cat <<'EOF'
{
  "status": "success",
  "data": {
    "passed": true,
    "feedback": ""
  },
  "error": null,
  "metrics": {"failed_tests": 0},
  "artifacts": []
}
EOF
fi
