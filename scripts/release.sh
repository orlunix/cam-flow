#!/usr/bin/env bash
# camflow release: build → test → deploy to remote machines → verify.
#
# Mirrors the camc/TeaSpirit remote release model. A relocatable release
# tree is built under dist/camflow-release and shipped to each ssh machine
# listed in ~/.cam/machines.json:
#
#   ~/.cam/camflow                  wrapper executable
#   ~/.cam/camflow-release/         source/assets tree used by wrapper
#
# Usage:
#   scripts/release.sh
#   scripts/release.sh --skip-tests
#   scripts/release.sh --skip-build
#   scripts/release.sh --only NAME[,NAME]
#   scripts/release.sh --dry-run
#   scripts/release.sh --help
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MACHINES_FILE="${CAMC_MACHINES_FILE:-$HOME/.cam/machines.json}"
DIST_DIR="$REPO_ROOT/dist"
WRAPPER="$DIST_DIR/camflow"
SINGLE_FILE="$DIST_DIR/camflow.py"
TARBALL="$DIST_DIR/camflow-release.tar.gz"
REMOTE_DIR="~/.cam"
REMOTE_WRAPPER="$REMOTE_DIR/camflow"
REMOTE_SINGLE="$REMOTE_DIR/camflow.py"
REMOTE_TARBALL="$REMOTE_DIR/camflow-release.tar.gz"
SHARED_BIN_DIR="/home/prgn_share/bin"
SHARED_BIN_PATH="$SHARED_BIN_DIR/camflow"
SHARED_SINGLE_PATH="$SHARED_BIN_DIR/camflow.py"
SHARED_CURRENT_DIR="/home/prgn_share/tools/camflow/current"
SHARED_RELEASES_DIR="/home/prgn_share/tools/camflow/releases"
SSH_TIMEOUT=10

SKIP_TESTS=0
SKIP_BUILD=0
DRY_RUN=0
ONLY=""

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log()  { printf '\033[1;34m[release]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[release]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[release]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[release]\033[0m %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) err "unknown flag: $1"; usage 1 ;;
  esac
done

cd "$REPO_ROOT"

if [[ $SKIP_BUILD -eq 1 ]]; then
  [[ -x "$WRAPPER" ]] || { err "--skip-build but $WRAPPER is missing"; exit 1; }
  [[ -x "$SINGLE_FILE" ]] || { err "--skip-build but $SINGLE_FILE is missing"; exit 1; }
  [[ -f "$TARBALL" ]] || { err "--skip-build but $TARBALL is missing"; exit 1; }
  log "skip build (using existing dist/)"
else
  build_args=()
  [[ $SKIP_TESTS -eq 1 ]] && build_args+=(--skip-tests)
  "$REPO_ROOT/scripts/build.sh" "${build_args[@]}"
fi

LOCAL_VERSION="$("$WRAPPER" version 2>/dev/null | head -1 | tr -d '\r')"
[[ -n "$LOCAL_VERSION" ]] || { err "dist/camflow did not print a version"; exit 1; }
ok "local: $LOCAL_VERSION"

VER_TOKEN="$(printf '%s' "$LOCAL_VERSION" | awk '{print $2}')"
SHORT_HASH="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
ARCHIVE_NAME="camflow-${VER_TOKEN:-unknown}${SHORT_HASH:+-$SHORT_HASH}.tar.gz"
SINGLE_ARCHIVE_NAME="camflow-${VER_TOKEN:-unknown}${SHORT_HASH:+-$SHORT_HASH}.py"

[[ -f "$MACHINES_FILE" ]] || { err "machines file not found: $MACHINES_FILE"; exit 1; }

MACHINES="$(
python3 - "$MACHINES_FILE" "$ONLY" <<'PY'
import json
import sys

path, only = sys.argv[1], sys.argv[2]
want = set(x.strip() for x in only.split(",") if x.strip()) if only else None
with open(path) as f:
    data = json.load(f)
for m in data:
    if m.get("type") != "ssh":
        continue
    name = m.get("name", "")
    if want is not None and name not in want:
        continue
    host = m.get("host", "")
    user = m.get("user", "")
    port = m.get("port") or ""
    if host:
        print("%s\t%s\t%s\t%s" % (name, host, user, port))
PY
)"

if [[ -z "$MACHINES" ]]; then
  err "no machines to deploy to"
  exit 1
fi

log "deploy targets:"
printf '%s\n' "$MACHINES" \
  | awk -F'\t' '{printf "   %-28s %s@%s:%s\n", $1, $3, $2, $4}'

cm_path() {
  local user="$1" host="$2" port="$3"
  [[ -z "$port" ]] && port=22
  python3 -c "
import hashlib, sys
h = hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12]
print('/tmp/cam-ssh-%s' % h)
" "${user}@${host}:${port}"
}

ssh_args() {
  local user="$1" host="$2" port="$3"
  local cm; cm="$(cm_path "$user" "$host" "$port")"
  local args=(
    -n
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout="$SSH_TIMEOUT"
    -o ControlPath="$cm"
    -o ControlMaster=auto
    -o ControlPersist=600
  )
  [[ -n "$port" ]] && args+=(-p "$port")
  printf '%s\n' "${args[@]}"
}

scp_args() {
  ssh_args "$@" | sed -e '/^-n$/d' -e 's/^-p$/-P/'
}

deployed=0
verified=0
failed=0
failures=()

while IFS=$'\t' read -r name host user port; do
  [[ -z "$name" ]] && continue
  target="${user:+${user}@}${host}"
  label="$name ($target${port:+:$port})"
  log "→ $label"

  mapfile -t SSH_OPTS < <(ssh_args "$user" "$host" "$port")
  mapfile -t SCP_OPTS < <(scp_args "$user" "$host" "$port")

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '   ssh %s %s "mkdir -p ~/.cam"\n' "${SSH_OPTS[*]}" "$target"
    printf '   scp %s %s %s %s %s:%s/\n' \
      "${SCP_OPTS[*]}" "$WRAPPER" "$SINGLE_FILE" "$TARBALL" "$target" "$REMOTE_DIR"
    printf '   ssh %s %s "tar -xzf %s -C %s && chmod +x %s && %s version && %s --help >/dev/null"\n' \
      "${SSH_OPTS[*]}" "$target" "$REMOTE_TARBALL" "$REMOTE_DIR" \
      "$REMOTE_WRAPPER" "$REMOTE_WRAPPER" "$REMOTE_WRAPPER"
    printf '   ssh %s %s "chmod +x %s && %s version && %s --help >/dev/null"\n' \
      "${SSH_OPTS[*]}" "$target" "$REMOTE_SINGLE" "$REMOTE_SINGLE" "$REMOTE_SINGLE"
    continue
  fi

  # Some machines have ~/.cam/camflow as a symlink to /home/prgn_share/bin.
  # Remove the link before scp so the private canary smoke uses the freshly
  # uploaded ~/.cam/camflow-release tree instead of the old shared install.
  if ! ssh "${SSH_OPTS[@]}" "$target" \
      "mkdir -p ~/.cam && rm -f ~/.cam/camflow ~/.cam/camflow.py ~/.cam/camflow-release.tar.gz" \
      >/dev/null 2>&1; then
    err "   mkdir ~/.cam failed on $label"
    failed=$((failed + 1)); failures+=("$name:mkdir"); continue
  fi

  if ! scp "${SCP_OPTS[@]}" "$WRAPPER" "$SINGLE_FILE" "$TARBALL" "$target:$REMOTE_DIR/" \
      </dev/null >/dev/null 2>&1; then
    err "   scp failed on $label"
    failed=$((failed + 1)); failures+=("$name:scp"); continue
  fi

  deploy_cmd="tar -xzf $REMOTE_TARBALL -C $REMOTE_DIR && chmod +x $REMOTE_WRAPPER $REMOTE_SINGLE"
  if ! ssh "${SSH_OPTS[@]}" "$target" "$deploy_cmd" >/dev/null 2>&1; then
    err "   extract/chmod failed on $label"
    failed=$((failed + 1)); failures+=("$name:extract"); continue
  fi
  deployed=$((deployed + 1))

  remote_ver="$(ssh "${SSH_OPTS[@]}" "$target" "$REMOTE_WRAPPER version" \
    2>/dev/null | head -1 | tr -d '\r' || true)"
  if [[ "$remote_ver" == "$LOCAL_VERSION" ]]; then
    if ! ssh "${SSH_OPTS[@]}" "$target" "$REMOTE_WRAPPER --help" \
        >/dev/null 2>&1; then
      warn "   runtime smoke failed for $label"
      failed=$((failed + 1)); failures+=("$name:runtime"); continue
    fi
    single_ver="$(ssh "${SSH_OPTS[@]}" "$target" "$REMOTE_SINGLE version" \
      2>/dev/null | head -1 | tr -d '\r' || true)"
    if [[ "$single_ver" != "$LOCAL_VERSION" ]]; then
      warn "   single-file version mismatch: expected '$LOCAL_VERSION', got '$single_ver'"
      failed=$((failed + 1)); failures+=("$name:single-mismatch"); continue
    fi
    if ! ssh "${SSH_OPTS[@]}" "$target" "$REMOTE_SINGLE --help" \
        >/dev/null 2>&1; then
      warn "   single-file smoke failed for $label"
      failed=$((failed + 1)); failures+=("$name:single-runtime"); continue
    fi
    ok "   $remote_ver"
    verified=$((verified + 1))
  else
    warn "   version mismatch: expected '$LOCAL_VERSION', got '$remote_ver'"
    failed=$((failed + 1)); failures+=("$name:mismatch"); continue
  fi

  if ssh "${SSH_OPTS[@]}" "$target" \
      "test -d $SHARED_BIN_DIR -a -w $SHARED_BIN_DIR" >/dev/null 2>&1; then
    install_cmd=\
"mkdir -p $SHARED_CURRENT_DIR && \
cp -p $REMOTE_WRAPPER $SHARED_CURRENT_DIR/camflow && \
rm -rf $SHARED_CURRENT_DIR/camflow-release && \
tar -xzf $REMOTE_TARBALL -C $SHARED_CURRENT_DIR && \
chmod 0755 $SHARED_CURRENT_DIR/camflow && \
ln -sfn $SHARED_CURRENT_DIR/camflow $SHARED_BIN_PATH && \
cp -p $REMOTE_SINGLE $SHARED_SINGLE_PATH && \
chmod 0755 $SHARED_SINGLE_PATH && \
$SHARED_BIN_PATH version >/dev/null && \
$SHARED_BIN_PATH --help >/dev/null && \
$SHARED_SINGLE_PATH version >/dev/null && \
$SHARED_SINGLE_PATH --help >/dev/null"
    if ssh "${SSH_OPTS[@]}" "$target" "$install_cmd" >/dev/null 2>&1; then
      ok "   shared: $SHARED_BIN_PATH -> $SHARED_CURRENT_DIR/camflow"
    else
      warn "   shared install failed for $label"
    fi
  fi

  archive_cmd="mkdir -p $SHARED_RELEASES_DIR && cp -p $REMOTE_TARBALL $SHARED_RELEASES_DIR/$ARCHIVE_NAME && cp -p $REMOTE_SINGLE $SHARED_RELEASES_DIR/$SINGLE_ARCHIVE_NAME"
  if ssh "${SSH_OPTS[@]}" "$target" "$archive_cmd" >/dev/null 2>&1; then
    ok "   archived: $SHARED_RELEASES_DIR/$ARCHIVE_NAME"
  else
    log "   (archive skipped: $SHARED_RELEASES_DIR not writable)"
  fi
done <<< "$MACHINES"

echo
printf '\033[1m%s\033[0m\n' "summary"
printf '   local version: %s\n' "$LOCAL_VERSION"
printf '   deployed     : %d\n' "$deployed"
printf '   verified     : %d\n' "$verified"
printf '   failed       : %d\n' "$failed"
if (( ${#failures[@]} > 0 )); then
  printf '   failed hosts : %s\n' "${failures[*]}"
  exit 1
fi

if [[ $DRY_RUN -eq 0 && $verified -gt 0 ]] && command -v git >/dev/null 2>&1 \
    && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  TS="$(date +%Y%m%d%H%M%S)"
  TAG="deploy-$TS"
  HOSTS="$(printf '%s\n' "$MACHINES" | awk -F'\t' 'NF && $1 {print "   - " $1}')"
  TAG_MSG="camflow deploy $TS

version: $LOCAL_VERSION
verified: $verified / failed: $failed
hosts:
$HOSTS"
  if git -C "$REPO_ROOT" tag -a "$TAG" -m "$TAG_MSG" 2>/dev/null; then
    ok "tagged: $TAG   (git show $TAG)"
  else
    warn "tag failed (tag may already exist): $TAG"
  fi
fi

exit 0
