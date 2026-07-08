#!/usr/bin/env bash
# Build CamFlow's readable Python 3.6+ standalone executable.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKIP_TESTS=0

case "${1:-}" in
  "") ;;
  --skip-tests) SKIP_TESTS=1 ;;
  -h|--help)
    printf 'usage: scripts/build.sh [--skip-tests]\n'
    exit 0
    ;;
  *)
    printf 'ERROR: unknown option: %s\n' "$1" >&2
    exit 1
    ;;
esac

cd "$REPO_ROOT"
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  python3 -m unittest tests.test_camflow_build -q
fi
exec python3 build_camflow.py --output dist/camflow
