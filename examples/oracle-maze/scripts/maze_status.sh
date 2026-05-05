#!/usr/bin/env bash
# maze_status — POST /tool tool=maze_status args={}. Read-only.
set -e

base="${CAMFLOW_ORACLE_BASE_URL:?CAMFLOW_ORACLE_BASE_URL must be set}"
session="${CAMFLOW_ORACLE_SESSION_ID:?CAMFLOW_ORACLE_SESSION_ID must be set}"
rev="${CAMFLOW_DAG_REVISION:-1}"

cat >/dev/null

resp=$(curl -fsS -X POST "$base/tool" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg s "$session" --argjson r "$rev" \
            '{session_id: $s, tool: "maze_status", dag_revision: $r, args: {}}')" \
    || echo '{"ok": false, "error": "curl failed"}')

jq -nc --argjson r "$resp" '{
  status: ( ($r.ok // false) | if . then "success" else "fail" end ),
  data: $r,
  error: ( ($r.ok // false) | if . then null else {code: "ORACLE_REJECT", message: ($r.error // "status failed")} end ),
  feedback: null,
  request_human: false
}'
