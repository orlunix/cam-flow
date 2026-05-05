#!/usr/bin/env bash
# maze_probe — POST /tool tool=maze_probe args={prefix:[...]}.
# stdin: JSON {prefix: [...]} (or {data: {prefix: [...]}} if upstream).
set -e

base="${CAMFLOW_ORACLE_BASE_URL:?CAMFLOW_ORACLE_BASE_URL must be set}"
session="${CAMFLOW_ORACLE_SESSION_ID:?CAMFLOW_ORACLE_SESSION_ID must be set}"
rev="${CAMFLOW_DAG_REVISION:-1}"

stdin_json=$(cat)
# Accept either top-level prefix or upstream-injected data.prefix.
prefix_json=$(jq -c '
    if .prefix then .prefix
    elif .data and .data.prefix then .data.prefix
    elif .upstream then ([ .upstream | to_entries[] | .value.data.prefix? ] | map(select(. != null)) | first // [])
    else []
    end' <<<"$stdin_json")

resp=$(curl -fsS -X POST "$base/tool" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg s "$session" --argjson r "$rev" --argjson pre "$prefix_json" \
            '{session_id: $s, tool: "maze_probe", dag_revision: $r, args: {prefix: $pre}}')" \
    || echo '{"ok": false, "error": "curl failed"}')

jq -nc --argjson r "$resp" '{
  status: ( ($r.ok // false) | if . then "success" else "fail" end ),
  data: $r,
  error: ( ($r.ok // false) | if . then null else {code: "ORACLE_REJECT", message: ($r.error // "probe failed")} end ),
  feedback: null,
  request_human: false
}'
