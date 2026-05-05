#!/usr/bin/env bash
# Build a relocatable CamFlow release artifact.
#
# Outputs:
#   dist/camflow                  wrapper executable
#   dist/camflow-release/         extracted release tree
#   dist/camflow-release.tar.gz   tarball for remote deployment
#   dist/camflow.py               zero-install single-file launcher
#
# Usage:
#   scripts/build.sh
#   scripts/build.sh --skip-tests
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
RELEASE_DIR="$DIST_DIR/camflow-release"
WRAPPER="$DIST_DIR/camflow"
TARBALL="$DIST_DIR/camflow-release.tar.gz"
SINGLE_FILE="$DIST_DIR/camflow.py"

SKIP_TESTS=0

usage() {
  sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log() { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1; shift ;;
    -h|--help) usage 0 ;;
    *) err "unknown flag: $1"; usage 1 ;;
  esac
done

cd "$REPO_ROOT"

VERSION="$(
python3 - <<'PY'
from pathlib import Path
import re

text = Path("pyproject.toml").read_text()
match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
if not match:
    raise SystemExit("could not find project.version in pyproject.toml")
print(match.group(1))
PY
)"
SHORT_HASH="$(git rev-parse --short HEAD 2>/dev/null || true)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ $SKIP_TESTS -eq 0 ]]; then
  log "running tests ..."
  PYTHONPATH=src python3 -m pytest tests/ -q
else
  log "skip tests (--skip-tests)"
fi

log "staging release tree ..."
rm -rf "$RELEASE_DIR" "$WRAPPER" "$TARBALL" "$SINGLE_FILE"
mkdir -p "$RELEASE_DIR"

cp -R src builtin skills "$RELEASE_DIR/"
cp pyproject.toml README.md LICENSE "$RELEASE_DIR/"
find "$RELEASE_DIR" -type d \( -name __pycache__ -o -name '*.egg-info' \) \
  -prune -exec rm -rf {} +

log "vendoring runtime dependencies ..."
mkdir -p "$RELEASE_DIR/vendor"
python3 - "$RELEASE_DIR/vendor" <<'PY'
from pathlib import Path
import shutil
import sys

vendor = Path(sys.argv[1])
try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required to build the release artifact") from exc

yaml_dir = Path(yaml.__file__).resolve().parent
target = vendor / "yaml"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(
    yaml_dir,
    target,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
PY
if [[ -d docs ]]; then
  mkdir -p "$RELEASE_DIR/docs"
  cp docs/spec.md "$RELEASE_DIR/docs/spec.md"
  cp docs/spec-1.1-goal-driven-supplement-2026-05-05.md \
     "$RELEASE_DIR/docs/spec-1.1-goal-driven-supplement-2026-05-05.md"
fi

cat > "$RELEASE_DIR/VERSION" <<EOF
version=$VERSION
commit=${SHORT_HASH:-unknown}
built_at=$BUILD_TIME
EOF

cat > "$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
RELEASE_DIR="$SCRIPT_DIR/camflow-release"
VERSION_FILE="$RELEASE_DIR/VERSION"

if [[ "${1:-}" == "version" || "${1:-}" == "--version" ]]; then
  if [[ -f "$VERSION_FILE" ]]; then
    version="$(sed -n 's/^version=//p' "$VERSION_FILE")"
    commit="$(sed -n 's/^commit=//p' "$VERSION_FILE")"
    built_at="$(sed -n 's/^built_at=//p' "$VERSION_FILE")"
    printf 'camflow v%s commit=%s built_at=%s\n' "$version" "$commit" "$built_at"
  else
    printf 'camflow version unknown\n'
  fi
  exit 0
fi

if [[ ! -d "$RELEASE_DIR/src" ]]; then
  echo "ERROR: missing CamFlow release tree: $RELEASE_DIR" >&2
  exit 1
fi

CAMFLOW_PYTHON="${CAMFLOW_PYTHON:-}"
if [[ -z "$CAMFLOW_PYTHON" ]]; then
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
             >/dev/null 2>&1; then
      CAMFLOW_PYTHON="$candidate"
      break
    fi
  done
fi

if [[ -z "$CAMFLOW_PYTHON" ]]; then
  echo "ERROR: CamFlow requires Python >= 3.10; set CAMFLOW_PYTHON to a suitable interpreter" >&2
  exit 1
fi

export PYTHONPATH="$RELEASE_DIR/vendor:$RELEASE_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$CAMFLOW_PYTHON" -m runner.runtime "$@"
EOF
chmod +x "$WRAPPER"

log "packing tarball ..."
tar -C "$DIST_DIR" -czf "$TARBALL" camflow-release

log "building single-file launcher ..."
python3 "$REPO_ROOT/scripts/build_single.py" \
  --release-dir "$RELEASE_DIR" \
  --output "$SINGLE_FILE" >/dev/null

log "local smoke ..."
"$WRAPPER" version >/dev/null
"$WRAPPER" --help >/dev/null 2>&1
"$SINGLE_FILE" version >/dev/null
"$SINGLE_FILE" --help >/dev/null 2>&1

ok "built $WRAPPER"
ok "built $TARBALL"
ok "built $SINGLE_FILE"
