#!/bin/bash
# Copy examples/value-demo/fixture/ to a fresh isolated directory.
# Each A/B leg uses a separate copy so the two sides can't contaminate
# each other.
set -e
DEST="$1"
if [ -z "$DEST" ]; then
    echo "Usage: $0 <dest-dir>" >&2
    exit 1
fi
if [ -e "$DEST" ]; then
    echo "ERROR: $DEST already exists. Pick a fresh path." >&2
    exit 1
fi
SRC=$(cd "$(dirname "$0")/.." && pwd)/fixture
cp -r "$SRC" "$DEST"
echo "Fixture copied to $DEST"
echo "  Files: $(find "$DEST" -type f | wc -l)"
echo "Next:"
echo "  cd $DEST"
echo "  # see examples/value-demo/AB-PROTOCOL.md for the run commands."
