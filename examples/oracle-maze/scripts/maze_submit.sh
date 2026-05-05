#!/usr/bin/env bash
# maze_submit — POST /tool tool=maze_submit args={path:[...], phrase?:str}.
# stdin: {path: [...], phrase?: ...} OR {data: {path: ...}} OR upstream
# carrying a path on a designated upstream node.
#
# IMPORTANT: oracle deliberately halts the FIRST submit on any session
# at dag_revision=1 even when the path is correct. The runtime turns
# our fail envelope into a workflow halt; the operator then runs
# `camflow replan` so this script is invoked again with
# CAMFLOW_DAG_REVISION>=2 and the oracle accepts.
set -e

base="${CAMFLOW_ORACLE_BASE_URL:?CAMFLOW_ORACLE_BASE_URL must be set}"
session="${CAMFLOW_ORACLE_SESSION_ID:?CAMFLOW_ORACLE_SESSION_ID must be set}"
rev="${CAMFLOW_DAG_REVISION:-1}"

stdin_json=$(cat)
path_json=$(jq -c '
    if .path then .path
    elif .data and .data.path then .data.path
    elif .upstream then ([ .upstream | to_entries[] | .value.data.path? ] | map(select(. != null)) | first // [])
    else []
    end' <<<"$stdin_json")
phrase=$(jq -r '
    if .phrase then .phrase
    elif .data and .data.phrase then .data.phrase
    else ""
    end' <<<"$stdin_json")

if [ "$phrase" = "" ] || [ "$phrase" = "null" ]; then
    body=$(jq -nc --arg s "$session" --argjson r "$rev" --argjson p "$path_json" \
            '{session_id: $s, tool: "maze_submit", dag_revision: $r, args: {path: $p}}')
else
    body=$(jq -nc --arg s "$session" --argjson r "$rev" --argjson p "$path_json" --arg ph "$phrase" \
            '{session_id: $s, tool: "maze_submit", dag_revision: $r, args: {path: $p, phrase: $ph}}')
fi

resp=$(curl -fsS -X POST "$base/tool" \
    -H 'content-type: application/json' \
    -d "$body" \
    || echo '{"ok": false, "error": "curl failed"}')

# Translate oracle response → CamFlow envelope.
# Three cases:
#   solved=true                 → success
#   halt=true                   → fail with feedback (triggers retry-or-halt)
#   error / not solved / other  → fail
jq -nc --argjson r "$resp" '
  ($r.solved // false) as $solved
  | (($r.halt // false) or ($r.replan_required // false)) as $halt
  | {
      status: ($solved | if . then "success" else "fail" end),
      data: $r,
      error: (
        if $solved then null
        elif $halt then {
          code: "ORACLE_HALT",
          message: ($r.message // "oracle requested replan; submit blocked at this revision")
        }
        else {
          code: "ORACLE_REJECT",
          message: ($r.error // $r.message // "submit rejected")
        }
        end
      ),
      feedback: ($r.feedback // $r.message // null),
      request_human: false
    }'
