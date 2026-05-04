#!/bin/bash
# Run pytest tests/ + tests/invariants/ at the project root.
# Used by the implementer node's verify.command. Exits non-zero on
# any test failure; the runtime captures stdout+stderr and feeds it
# back as previous.feedback on the next attempt.
set -e
P=$PWD
while [ ! -f "$P/SPEC.md" ] && [ "$P" != "/" ]; do
    P=$(dirname "$P")
done
[ "$P" = "/" ] && {
    echo "ERROR: project root (SPEC.md) not found walking up from $PWD" >&2
    exit 2
}
cd "$P"
exec pytest tests/ tests/invariants/ -q --tb=short
