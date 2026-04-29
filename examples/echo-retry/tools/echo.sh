#!/usr/bin/env bash
# Demo tool: read input JSON from stdin, return a success envelope echoing it.
set -euo pipefail
input=$(cat)
cat <<EOF
{
  "status": "success",
  "data": {
    "echoed": $input
  },
  "error": null,
  "metrics": {},
  "artifacts": []
}
EOF
