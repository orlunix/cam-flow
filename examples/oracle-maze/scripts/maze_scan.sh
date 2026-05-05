#!/usr/bin/env bash
# maze_scan — POST /tool tool=maze_scan args={}.
# Emits a CamFlow envelope to stdout. dag_revision comes from
# CAMFLOW_DAG_REVISION (set by exec_tool); BASE_URL + SESSION_ID
# from the env (caller exports them).
set -e

base="${CAMFLOW_ORACLE_BASE_URL:?CAMFLOW_ORACLE_BASE_URL must be set}"
session="${CAMFLOW_ORACLE_SESSION_ID:?CAMFLOW_ORACLE_SESSION_ID must be set}"
rev="${CAMFLOW_DAG_REVISION:-1}"

# Drain stdin (input.json from runtime); we don't need it for scan.
cat >/dev/null

resp=$(curl -fsS -X POST "$base/tool" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg s "$session" --argjson r "$rev" \
            '{session_id: $s, tool: "maze_scan", dag_revision: $r, args: {}}')" \
    || echo '{"ok": false, "error": "curl failed"}')

# Wrap the oracle response in a CamFlow envelope. data.ok = oracle's ok.
jq -nc --argjson r "$resp" '{
  status: ( ($r.ok // false) | if . then "success" else "fail" end ),
  data: $r,
  error: ( ($r.ok // false) | if . then null else {code: "ORACLE_REJECT", message: ($r.error // "scan failed")} end ),
  feedback: null,
  request_human: false
}'
