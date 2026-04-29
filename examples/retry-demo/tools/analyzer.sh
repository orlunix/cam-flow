#!/usr/bin/env bash
# Always returns the same root_cause; demonstrates a deterministic analysis step.
set -euo pipefail
cat > /dev/null
cat <<'EOF'
{
  "status": "success",
  "data": {
    "root_cause": "Parser.tokenize dereferences input without null check"
  },
  "error": null,
  "metrics": {},
  "artifacts": []
}
EOF
