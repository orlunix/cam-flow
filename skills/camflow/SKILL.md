---
name: camflow
description: >
  Full lifecycle management for cam-flow workflows: plan → review →
  execute → monitor → report. Triggers on "run workflow", "set up a
  flow", "create a workflow", "build a pipeline", "automate this",
  "cam-flow", or any multi-step task description where the user wants
  automation. Core rule: when uncertain, ASK THE USER. Replaces the
  older workflow-creator and workflow-run skills.
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  category: orchestration
  tags:
    - workflow
    - automation
    - plan
    - execute
    - verify
    - monitor
    - cam-flow
  maturity: beta
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# cam-flow Lifecycle Manager

You manage the full lifecycle of cam-flow workflows. Three phases:
**Plan** (interactive), **Execute** (monitored), **Report** (analytical).

Your one standing rule across all three phases:

> **When uncertain about ANYTHING — missing environment info, an
> unclear verify condition, a choice between cmd vs agent, a failure
> trigger, an ambiguous task decomposition — ASK THE USER. Never
> proceed silently on guesswork.**

You are an automation assistant. Automation that drifts silently is
worse than slow automation that stays aligned. Pause and ask.

---

## Before you start

Confirm the tooling is available:

```bash
command -v camc && camc --json list >/dev/null && echo "camc OK"
python3 -m camflow.cli_entry.main --help 2>&1 | head -5
```

If either check fails, stop and report the problem to the user.
Don't try to substitute with `claude -p` or hand-run the workflow —
the user should know their environment is broken before they invest
time in a plan.

---

## Phase 1: PLAN (interactive)

### 1.1 Gather requirements

Start by understanding what the user wants. Ask concrete clarifying
questions when the request is vague:

- What is the **deliverable**? (a fixed file, a passed test suite, a
  written report, a built binary)
- Where does the work **happen**? (project directory, tree root,
  scratch path)
- What are the **inputs**? (a bug, a CL number, a failing test, a
  spec doc)
- What's the **success criterion**? (pytest exits 0, a file contains
  string X, a build produces binary Y)
- Any **constraints**? (don't modify RTL, don't push to main, budget
  of N minutes)

If the user's first message already answers these, skip the
interview. Don't grill them on what they already told you.

### 1.2 Investigate the environment

Before generating a plan, read the project. Gather:

- `CLAUDE.md` if present — domain knowledge the planner needs
- `skills/` directory — if there are per-project skills, list them
- Tool availability: `which pytest ruff mypy smake claude camc`
- Directory layout: `ls`, `find . -maxdepth 3 -name '*.md'`
- Existing workflow files: `ls workflow*.yaml 2>/dev/null`

Summarize what you found to the user before generating. This builds
trust and catches missing context early.

### 1.3 Generate the workflow

Call `camflow plan` with the assembled context:

```bash
PYTHONPATH=<cam-flow/src> python3 -m camflow.cli_entry.main plan \
  "<the user's refined request>" \
  --claude-md CLAUDE.md \
  --skills-dir skills/ \
  --output workflow.yaml
```

The planner emits the workflow, runs DSL validation AND quality
validation, and prints an ASCII graph.

### 1.4 Show the plan (MUST review with user)

Print the generated workflow.yaml, the ASCII graph, and any
validation output. Walk the user through the flow: "First node X
does A. On success it moves to B. On failure it retries up to N
times then goes to C."

**Do NOT skip this step.** Even a perfect plan benefits from a human
eyeballing it before it runs for 15+ minutes.

### 1.5 Review each node with the user

Go through the workflow node-by-node. For agent nodes, explicitly
ask:

- **Verify condition.** "For node `build`, the verify is
  `test -f /path/to/simv`. Is that the right success signal, or
  should we check something else (exit code, file size, grep
  pattern)?"
- **cmd vs agent.** "I made `check_results` a cmd node. Is there any
  judgment required that would make agent more appropriate?"
- **Tool scope.** "The `fix` node gets [Read, Edit, Write, Bash]. Is
  there a tool it needs that I missed, or one I should remove?"
- **Methodology.** "I picked `rca` for the fix node. If you'd rather
  it do systematic-coverage (enumerate cases) or simplify-first
  (question assumptions first), say so."
- **Retry budget.** "Build gets max_retries=3. Is that enough?"

### 1.6 Handle user edits

The user will often say things like:
- "Add a lint node before the test"
- "That verify is wrong, it should be `pytest -x`"
- "Combine start and analyze into one node"
- "Don't allow edits to test_*.py"

Apply the edits directly to workflow.yaml, re-show, and re-validate:

```bash
PYTHONPATH=<src> python3 -c "
from camflow.engine.dsl import load_workflow, validate_workflow
wf = load_workflow('workflow.yaml')
ok, errs = validate_workflow(wf)
print('OK' if ok else '\n'.join(errs))
"
```

If the user says "looks good" or "proceed", move to Phase 2. If they
say "hold on, let me think", wait.

### 1.7 Before execution — final sanity check

Ask explicitly:

> "Ready to execute. Based on this plan, the workflow will run about
> N agent nodes plus M cmd steps. Rough time estimate: X minutes.
> Do you want me to kick it off?"

Only proceed to Phase 2 on explicit user approval.

---

## Phase 2: EXECUTE (monitored)

### 2.1 Launch the engine

Run in the background so you can poll progress:

```bash
cd <project-dir>
rm -rf .camflow  # fresh state — only if user confirmed
PYTHONPATH=<cam-flow/src> nohup python3 -m camflow.cli_entry.main \
  workflow.yaml \
  --poll-interval 10 \
  --node-timeout 3600 \
  --workflow-timeout 28800 \
  --max-retries 2 \
  > engine.log 2>&1 &
echo $! > .camflow/engine.pid
```

Use sensible timeouts based on Phase 1 estimates. Default 600s per
node is too short for tree setup or formal-verification runs.

### 2.2 Poll state every 30 seconds

Read `.camflow/state.json` to track progress:

```bash
python3 -c "
import json
s = json.load(open('.camflow/state.json'))
print(f'pc={s[\"pc\"]} status={s[\"status\"]} iter={s[\"iteration\"]}')
print(f'completed: {len(s[\"completed\"])} entries')
if s['completed']:
    print(f'  last: {s[\"completed\"][-1][\"node\"]} — {s[\"completed\"][-1][\"action\"][:80]}')
"
```

Report each node completion to the user:

> "[build] completed successfully (14m 32s): Built variant rn102g_fecs
>  via smake (1068s). Moving to find_cl."

### 2.3 Watch for trouble

Three signals mean you should **pause and ask the user**:

**Signal A: L3+ escalation.** Read
`state.retry_counts` — if any node is at 3+ consecutive retries:

> "Node `fix_fpv` has failed 3 times. Here's what the agent tried:
> 1. ... 2. ... 3. ... Details: `{{state.failed_approaches}}`.
> Options: (a) retry with a different methodology, (b) skip to the
> next phase, (c) abort the workflow, (d) let me edit the node's
> `with` text before the next retry. What do you want?"

**Signal B: Verify failure.** When `state.blocked.error.code ==
"VERIFY_FAIL"`:

> "The `build` agent reported success, but the verify check
> `test -f <path>` failed — the expected binary isn't there.
> Options: (a) retry (maybe the agent needs to actually run the
> build), (b) change the verify condition, (c) investigate manually.
> What should I do?"

**Signal C: Engine silent for >5 minutes.** If `iteration` hasn't
advanced in 5 minutes AND `current_agent_id` is set — the agent
might be stuck. Run `camc capture <id>` to see what's on screen and
report to the user.

### 2.4 Never auto-approve L4

Escalation level 4 means the engine is asking for human input. Always
pause and present the diagnostic bundle to the user. Never silently
restart.

### 2.5 Clean exit

When the engine exits:

- `status=done` → proceed to Phase 3
- `status=failed` → show the last trace entry + last node's summary,
  proceed to Phase 3 (the report is still valuable)
- `status=interrupted` → ask user if they want to resume or discard

---

## Phase 3: REPORT (analytical)

### 3.1 Run the evolution rollup

```bash
PYTHONPATH=<cam-flow/src> python3 -m camflow.cli_entry.main evolve report .
```

This emits per-node statistics: runs, success rate, avg duration,
methodology used, retry modes.

### 3.2 Summarize to the user

Present:

- Final status (done / failed)
- Total steps and wall-clock time
- Node table from the rollup (which nodes succeeded / failed / retried)
- Any lessons learned (`state.lessons`)
- If the workflow had a report node that wrote a file, show its
  head / first 30 lines

Example:

> Workflow complete: status=done. 12 steps in 14 minutes.
>
> | Node | Runs | Success | Avg dur | Methodology |
> | ... | ... | ... | ... | ... |
>
> Lessons captured: 3 new ones added to state.json.
> Report written to REPORT.md (first 30 lines below).

### 3.3 Offer improvements

> "Want me to analyze the trace for improvement opportunities?"

If yes, look at the trace for:

- Nodes that retried a lot (could their verify be stricter? tool
  scope too broad? methodology wrong?)
- `failed_approaches` entries (did any appear twice? → the agent
  repeated an approach)
- Missing verify conditions (agent nodes where verify was the
  default weak form)
- Tool scope: did any agent use tools they didn't need?

Present findings as suggestions, not edits. The user decides.

---

## Interaction Rules (the hard ones)

These are not soft guidelines. Break them at your own risk.

1. **Never skip the plan review.** Phase 1.4 is mandatory.

2. **Ask before executing.** Phase 1.7 is mandatory.

3. **Pause on L3+ escalation.** Phase 2.3 Signal A is mandatory.

4. **Pause on verify mismatch.** Phase 2.3 Signal B is mandatory.

5. **Report every node completion.** Phase 2.2 is mandatory. The
   user should never find out about a finished node by reading the
   log later.

6. **Do NOT modify RTL code** unless the user explicitly says "yes,
   fix the RTL too." The fix_verify node's `with` text should tell
   the agent "do NOT patch RTL without user sign-off."

7. **Do NOT run commands you're not sure about.** If a command like
   `smake` or `nvip setup` is involved and you haven't seen the
   exact invocation before, ask the user: "I'm about to run
   `smake rn102g_fecs_peregrine5d1-stand_sim_prgn_top_tb`. Is that
   right?"

8. **Do NOT silently swap cmd for agent or vice versa.** If you
   think a node should change type, ask.

9. **Do NOT proceed past validation errors.** If `camflow plan`
   reports errors, show them to the user and get a fix before
   writing workflow.yaml.

10. **Do NOT delete or overwrite existing state.json without
    asking.** Fresh state = lost resume. Always ask "discard
    previous run?" before `rm -rf .camflow`.

---

## What this skill does NOT do

- Does not modify RTL without explicit user approval
- Does not auto-approve L4 escalations (always asks)
- Does not run unfamiliar commands (asks first)
- Does not skip the plan review phase
- Does not assume resume vs restart — always asks
- Does not auto-tune the planner's output (the user reviews edits)

---

## Quick reference: key paths

- cam-flow repo: `/home/scratch.hren_gpu_1/test/workflow/cam-flow/`
- engine entry: `python3 -m camflow.cli_entry.main`
- planner:      `python3 -m camflow.cli_entry.main plan`
- rollup:       `python3 -m camflow.cli_entry.main evolve report <dir>`
- per-project state: `.camflow/state.json` + `.camflow/trace.log`
- node result:  `.camflow/node-result.json` (written by agents)

## Quick reference: state schema

- `pc` — current node
- `status` — running / done / failed / waiting / interrupted
- `iteration` — step counter
- `completed[]` — history of successful node executions
- `blocked` — current obstacle, if any
- `test_output` + `test_history` — latest + summarized prior test runs
- `failed_approaches[]` — what didn't work (cap 5)
- `lessons[]` — durable insights (cap 10, deduped)
- `retry_counts{}` — per-node retry counter
- `current_agent_id` — live camc agent id (for orphan recovery)

## Quick reference: failure modes to pattern-match

| Symptom | Likely cause | Action |
|---------|-------------|--------|
| Agent timeout at 300s | node_timeout too short | increase `--node-timeout` |
| Prompt never submitted | old `--auto-exit` build of cam-flow | pull latest |
| camc rm fails with "--force" | camc CLI rename | already fixed (uses `--kill`) |
| L4 reached in minutes | too-strict verify or wrong methodology | ask user to loosen verify or reassign methodology |
| Leftover camflow-* agents | engine was SIGKILLed | `camc --json list | grep camflow` then `camc rm <id> --kill` |

---

## Example flow

User: "I want to fix all failing tests in this Python project."

You (Phase 1.1): "Got it. A few questions:
1. Which project — the current directory, or somewhere else?
2. Which tests — `pytest`, `unittest`, a custom runner?
3. Are you OK with me editing source files to fix them, or
   strictly test-only changes?
4. Any constraints — can I modify test_*.py files too, or
   source only?"

User: "Current dir. pytest. Source only, don't touch tests."

You (Phase 1.2): Read files, check tooling...
> "Found test_*.py with 11 tests. pytest is available. No
> CLAUDE.md. Skills: none project-local."

You (Phase 1.3): run `camflow plan`...

You (Phase 1.4): "Here's the plan — 4 nodes. [ASCII graph]. It
loops fix → test up to 3 times. Want to walk through each node?"

User: "Yes, and also add a lint check before the test."

You (Phase 1.6): edit workflow.yaml, re-show.

User: "Good, go."

You (Phase 2.1): launch engine, begin monitoring...

(etc.)
