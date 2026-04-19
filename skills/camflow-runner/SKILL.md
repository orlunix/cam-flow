---
name: camflow-runner
description: >
  Execute exactly ONE node of a cam-flow CLI-mode workflow, update
  state, then exit. Designed to be called by /loop for continuous
  execution — /loop calls back after each tick for the next node.
  Reads .claude/state/workflow.json, identifies the current node
  from workflow.yaml, executes it (cmd via Bash, agent via the
  current Claude session), captures the result, enriches state,
  resolves the next transition, and writes back. Triggers on
  "run workflow", "/workflow-run", "next step", "continue",
  "camflow-runner", or via /loop camflow-runner. Supersedes the
  older workflow-run skill (same file layout, same contract,
  updated to the six-section state schema and plan-priority fields).
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  category: orchestration
  tags:
    - workflow
    - automation
    - runner
    - cli-mode
    - cam-flow
  maturity: beta
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Skill
---

# camflow-runner (CLI mode, single-tick)

Run exactly ONE workflow node, update state, then STOP. The `/loop`
scheduler calls you again for the next node.

You are the CLI-mode executor. You are NOT the workflow controller —
the workflow.yaml is. Your job per tick:

1. Read state
2. Identify the current node
3. Execute it (or invoke the agent to)
4. Capture the result + enrich state
5. Resolve the next transition
6. Write state + trace
7. Exit

Don't loop. Don't execute more than one node. `/loop` handles the
repetition.

---

## Procedure

### 1. Read state

```bash
cat .claude/state/workflow.json
```

- File missing → initialize: `{"pc": "start", "status": "running"}`
  and write it. (camflow-creator should have done this in setup,
  but be defensive.)
- `status = done`      → reply "Workflow completed." and STOP.
- `status = failed`    → reply "Workflow failed at node <pc>." and
  STOP. Show the last trace entry.
- `status = waiting`   → reply "Workflow waiting for external
  event." and STOP.
- `status = running`   → proceed.

### 2. Read the current node

```bash
# Load workflow and find the node matching state.pc
```

Parse `workflow.yaml`. Find the node whose id equals `state.pc`.

Resolve `{{state.x}}` template variables in the node's `with` and
`do` fields against the current state dict. Do a simple string
substitution — no eval, no complex rendering.

If a `{{state.x}}` reference has no matching state key, leave the
placeholder in place so the error surfaces in the next step rather
than hiding.

### 3. Execute the node

**`cmd <command>`** — run the shell command. Exit 0 → success;
non-zero → fail. Capture stdout (last 2000 chars) and stderr
(last 500 chars).

```bash
bash -c "<resolved command>"
```

**`agent claude`** — the `with` field is YOUR task this tick. You
are the agent. Read files, write code, run commands, whatever is
needed to complete the task described in `with`. Apply any
`methodology` hint if the plan specifies one.

**`subagent claude`** — spawn a subagent via the Agent() tool. Pass
the `with` field as its task prompt. The subagent runs in isolated
context — it has no memory of your conversation. When it returns,
use its result to determine success/fail.

**`skill <name>`** — invoke the named skill via the Skill() tool:
`Skill("<name>")`. Pass the `with` field as context before
invocation.

Determine success/fail:

- `cmd`: exit code (0 = success, non-zero = fail)
- agent / subagent / skill: you decide based on whether the task
  was actually completed

Build a result dict:

```json
{
  "status": "success" | "fail",
  "summary": "<one sentence describing what happened>",
  "state_updates": {
    "...": "key-value pairs to merge into state"
  },
  "error": null | {"code": "...", "message": "..."}
}
```

### 4. Honor `verify` if the node has one

If the node declares a `verify` shell command AND the result is
`status=success`, run the verify cmd:

```bash
bash -c "<verify cmd with state vars resolved>"
```

Non-zero exit → downgrade the result to `status=fail` with
`error.code = "VERIFY_FAIL"` and summary noting the mismatch. This
mirrors the CAM engine's post-agent verify hook — the contract is
the same: agent claimed success, verify overrides.

### 5. Enrich state

Merge the node result into state. Keep it simple (CLI mode doesn't
run the full CAM `enrich_state`, but we capture the essentials):

- Apply `state_updates` via `state.update(result.state_updates)`
- Increment `state.iteration` by 1
- Append to `state.completed[]`:
  `{"node": <pc>, "action": summary, "iteration": ...}` (cap at 20,
  drop oldest)
- If agent reported `new_lesson`, append to `state.lessons[]` (dedup
  by exact string, cap at 10, FIFO prune)
- For `cmd` nodes, store last stdout tail as
  `state.last_cmd_output`; on fail also `state.test_output` so the
  next fix node sees it
- On fail, set `state.blocked = {"node": <pc>, "reason": summary}`
  and append to `state.failed_approaches[]` (cap 5); on success,
  clear `state.blocked`

### 6. Resolve transition (first match wins)

This is the same priority chain the CAM engine uses. First match
wins:

1. Abort control → `status = aborted`, STOP
2. Wait control → `status = waiting`, STOP
3. `transitions: [- if: fail]` and result is fail → goto that target
4. `transitions: [- if: success]` and result is success → goto
5. `transitions: [- if: output.<k>]` truthy → goto
6. `transitions: [- if: state.<k>]` truthy → goto
7. Goto control → goto target
8. Explicit `next` → goto that node
9. Default: on success → `status = done`; on fail → `status = failed`

### 7. Write state and trace

Merge everything into state, then atomically write
`.claude/state/workflow.json`:

```python
# Pseudo-python (your actual tool calls will be Bash + Write):
state["pc"] = <next_node_id_or_null>
state["status"] = <new_workflow_status>
# ... and all the enrichment above ...
```

Append to `.claude/state/trace.log` (JSONL, one entry per tick):

```json
{"iteration": <N>, "pc": "<node_id>", "next_pc": "<next|null>",
 "status": "<success|fail>", "summary": "...",
 "reason": "<why this transition fired>",
 "ts": <unix_seconds>}
```

Loop detection: before executing, check trace.log. If the current
node has appeared in `state.failed_approaches` 3+ times, set
`status = failed` and STOP with:

> "Workflow stuck: node `<pc>` failed 3+ times. Stopping for user
> review."

### 8. Report and exit

Print a one-line status for the user:

```
[<node_id>] <success|fail> → <next|done> (<reason>)
```

Then STOP. Do NOT continue to the next node yourself. `/loop` will
call you again after the tick interval.

---

## Plan-priority fields (if present)

The workflow may carry the same plan-priority fields the CAM engine
honors. In CLI mode we handle them more lightly:

- **`methodology`** — if present, prefix the agent's task with a
  methodology hint from the cam-flow `methodology_router` labels
  (rca / simplify-first / search-first / working-backwards /
  systematic-coverage).
- **`escalation_max`** — track `state.retry_counts[node_id]` and
  cap warning intensity at this level (e.g. if set to 0, never
  emit "try a different approach" banners on retry).
- **`max_retries`** — for a failing node, if
  `state.retry_counts[node_id] >= max_retries`, mark as exhausted
  and fall through to the `if fail` transition instead of retrying.
- **`allowed_tools`** — advisory only in CLI mode; print "this
  node's plan restricts tools to: [...]" and let the user / loop
  driver respect the boundary.
- **`verify`** — honored per Step 4 above.
- **`timeout`** — cmd nodes use this as `timeout <N>s <cmd>`; agent
  tasks are time-bounded by the /loop tick.

---

## Example trace (calculator demo)

```
iter 1  [start]  success → fix       (next)
iter 2  [fix]    success → test      (next)
iter 3  [test]   fail    → fix       (if fail)
iter 4  [fix]    success → test      (next)
iter 5  [test]   success → done      (if success)
iter 6  [done]   success → null      (workflow done)
```

Six ticks, six `/loop` invocations of camflow-runner. Each one
does ONE node and exits.

---

## Interaction rules

1. **Do exactly ONE node per invocation.** Never process more.
2. **Never silently skip the verify step** if the node declares
   one — that's plan-priority.
3. **Never mutate trace.log retroactively.** Append-only.
4. **Never overwrite state.json without reading first** — partial
   updates lose data.
5. **On loop detection (3+ consecutive fails)**, stop and report to
   the user. Do not keep retrying.
6. **If the workflow is already `done`/`failed`/`waiting`**, STOP
   and report. Do not resurrect.
7. **On `fail` result, set `state.blocked`** so the next fix agent
   sees what broke. On `success`, clear `state.blocked`.

---

## Relationship to workflow-run

camflow-runner supersedes the older `workflow-run` skill. File
layout and contract are identical (`.claude/state/workflow.json`,
`.claude/state/trace.log`, per-tick single-node execution) but this
version:

- Honors `verify` fields (post-agent gate)
- Handles `methodology` hints
- Caps retries at `max_retries` if the plan declares it
- Maintains the six-section state shape
  (`completed`/`blocked`/`lessons`/`failed_approaches`/...) so a
  workflow can be migrated between CLI and CAM modes without
  losing context

You can still use `workflow-run` for bare-minimum CLI flows that
don't need plan-priority fields. Prefer camflow-runner for anything
new.

---

## What this skill does NOT do

- Does not run the CAM engine (that's a separate Python process)
- Does not spawn camc sub-agents (CAM mode only)
- Does not modify workflow.yaml itself
- Does not skip verify checks
- Does not process more than one node per tick
