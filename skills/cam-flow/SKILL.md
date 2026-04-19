---
name: cam-flow
description: >
  DEPRECATED — split into camflow-creator (setup, Steps 0–6) and
  camflow-runner (CLI per-tick execution, the workflow-run
  replacement). Use camflow-creator for new projects; it handles both
  CLI and CAM modes. This skill is retained for reference and backward
  compatibility; new sessions should prefer the two-skill split.
  Original scope: set up and launch a cam-flow workflow project
  supporting CLI (agent drives /loop) and CAM (engine spawns
  sub-agents) modes. Gathered requirements, generated a plan via the
  camflow planner, reviewed with the user, wrote project files, and
  launched execution — then exited.
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
    - cam-flow
  maturity: beta
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# cam-flow Lifecycle Manager (setup + launch + exit)

You set up cam-flow workflow projects and launch them. After launch,
**you exit**. Do not stay running to babysit the engine — the engine
runs independently as a separate process (CAM mode) or the user drives
it via /loop (CLI mode). The user (or a future agent invocation) can
ask for a status report later; that's a separate job, covered in
Step 7.

Your one standing rule:

> **When uncertain about ANYTHING — missing environment info, unclear
> verify condition, cmd-vs-agent choice, ambiguous decomposition,
> unfamiliar command — ASK THE USER. Never guess. Never proceed
> silently.**

---

## Step 0: Determine execution mode

Two modes, pick one with the user:

- **CLI mode** — simple tasks (< 5 nodes), no formal verification
  needed, user wants to stay in the current Claude Code session.
  Agent runs nodes itself via `/loop /workflow-run`. Minimal YAML:
  just `do` / `with` / `next` / `transitions`.
- **CAM mode** — complex tasks (5+ nodes), needs verify conditions,
  needs trace / checkpoint, long-running. A Python engine process
  runs independently and spawns a fresh camc sub-agent per node.
  Rich YAML with methodology / escalation_max / allowed_tools /
  max_retries / verify per agent node.

If the choice isn't obvious, ask the user explicitly:

> "This looks like a [simple / complex] task. CLI mode runs it in
> your current session, CAM mode runs it as a background engine with
> full verification. Which do you prefer?"

**Default: CAM mode** for anything with loops, verify conditions, or
build / test / deploy steps. CLI mode is the exception, not the rule.

---

## Step 1: Gather requirements (interactive)

Interview the user. Adapt to what they've already told you; don't
grill them on answered questions.

**Must know (both modes):**

1. **Goal.** What's the end result? ("all tests pass", "verification
   report landed", "code deployed to staging")
2. **Working directory.** Where should the project live? Suggest a
   path based on context (scratch disk for big trees, repo for
   lightweight projects).
3. **Steps.** What are the main stages? If the user gives a vague
   "fix this and deploy it", help decompose into explicit stages.
4. **Key resources.** Existing code, repos, trees, tools, P4 CLs to
   reference?

**CAM mode adds:**

5. **Verify conditions.** For EACH step, how do we KNOW it succeeded?
   Ask explicitly:
   > "For the build step, how should I verify it worked? Check for a
   > binary file? An exit code? A grep pattern in the log?"
6. **Failure handling.** "If the build fails, should I retry? How
   many times? Should the retry be a blind retry or should I ask an
   agent to diagnose first?"
7. **Tool scope.** "Does this step need web access? Just file
   reading? Full edit + bash capability? Any tools it should NOT
   use?"

**CLI mode stays simple.** Goal, steps, working dir. Nothing else.

After every 2–3 answers, summarize back to the user what you
understand. Lets them course-correct before you burn effort in the
wrong direction.

---

## Step 2: Investigate the environment

Before generating a plan, check what's actually available. Don't
guess; read the project.

```bash
# Working dir contents
ls <project-dir>/ 2>/dev/null

# Tools likely to show up in the plan
for t in git python3 pytest ruff mypy smake vcs jg p4 claude camc; do
  which "$t" 2>/dev/null | head -1
done

# Free disk (tree setup can need tens of GB)
df -h <project-dir> 2>/dev/null

# Existing files the planner should know about
ls <project-dir>/CLAUDE.md \
   <project-dir>/workflow.yaml \
   <project-dir>/workflow-*.yaml \
   2>/dev/null

# For P4 tasks
p4 info 2>/dev/null | head -5
```

Summarize the findings to the user: "Found existing tree at X,
Python 3.12, 990 GB free, no CLAUDE.md yet." This builds trust and
catches missing context early.

If there's already a `workflow.yaml` in the directory, **ask**:

> "Found existing workflow.yaml. Run it as-is, adapt it, or create a
> new one from scratch?"

---

## Step 3: Generate the plan

### CLI mode

Write `workflow.yaml` directly. Simple structure:

```yaml
start:
  do: agent claude
  with: |
    <task description with {{state.*}} refs>
  next: test

test:
  do: cmd <test-command>
  transitions:
    - if: fail
      goto: start
    - if: success
      goto: done

done:
  do: cmd echo "done"
```

No `verify` / `methodology` / `escalation_max` / `allowed_tools` /
`max_retries` needed — CLI mode trusts the user-in-the-loop.

### CAM mode

Use the planner:

```bash
cd <project-dir>
PYTHONPATH=/home/scratch.hren_gpu_1/test/workflow/cam-flow/src \
  python3 -m camflow.cli_entry.main plan \
  "<user's refined request>" \
  --claude-md <project-dir>/CLAUDE.md \
  --output <project-dir>/workflow.yaml
```

The planner runs one strong-model call, validates both DSL and plan
quality, writes the YAML, and prints an ASCII graph + any warnings.

If the planner isn't available (no CLAUDE.md yet, no API key, LLM
call fails), fall back to writing workflow.yaml by hand using these
rules:

- Every **agent** node MUST carry: `verify`, `methodology`,
  `escalation_max`, `allowed_tools`, `max_retries`.
- Use **cmd** for deterministic ops (build, test, file check, grep).
- Use **agent** for creative / analytical work.
- Methodology picks (clearly labeled so the author knows what fits):
  - `simplify-first` — env setup, build, deploy, write report
  - `search-first` — research, code analysis, reading RTL
  - `rca` — debugging, fixing test failures, diagnosing errors
  - `working-backwards` — design, planning, verification plans
  - `systematic-coverage` — running tests, executing verification,
    code review
- Allowed tool recipes:
  - fix nodes: `[Read, Edit, Write, Bash]`
  - analysis nodes: `[Read, Glob, Grep, Bash]`
  - planning nodes: `[Read, Write]`
  - test/run nodes: usually `cmd` — no agent, no tool set
- Verify-condition cookbook:
  - File created:   `test -f <path>`
  - File non-empty: `test -s <path>`
  - Value set:      `test -n "{{state.key}}"`
  - Tests pass:     the same test command that proves success
  - Code landed:    `grep -q "<pattern>" <file>`

Show the generated plan to the user.

---

## Step 4: Review with the user (CRITICAL — never skip)

Present the plan in a structured way:

```
=== Workflow Plan (CAM mode) ===

  setup-tree → build → find-cl → analyze → plan-verify →
  run-verify → report → done
                  ↑                       │fail
                  └─────── fix ◄──────────┘

Nodes: 9 (6 agent, 2 cmd, 1 done)
Estimated time: ~60 min
Retry cap: 3 per node, 10 total executions

Node details:
  1. setup-tree    agent   simplify-first      max_retries=2
     Verify: test -d <tree>/hw/nvip/ip/peregrine/5.1/vmod
     Tools:  [Read, Bash, Write]

  2. build         agent   simplify-first      max_retries=3
     Verify: test -n "{{state.build_ok}}"
     Tools:  [Read, Bash, Edit]

  ... (one block per node) ...

  9. done          cmd     cat REPORT.md
```

Then for EACH agent node, ask explicit questions:

- "Is the verify condition for `<node>` correct? (`<verify cmd>`)"
- "Should `<node>` be cmd instead of agent? It looks fairly
  deterministic."
- "Any domain knowledge I should add to CLAUDE.md so the `<node>`
  agent knows what `<tool>` does in this project?"

If the user changes anything — **regenerate and re-show**. Don't
apply one edit, move on, and silently ignore the rest.

Do NOT proceed past this step until the user says "looks good",
"approved", "go", or equivalent. Silence is not approval.

---

## Step 5: Set up project files

Build the complete project directory:

```
<project-dir>/
├── CLAUDE.md              ← domain knowledge for sub-agents
├── workflow.yaml          ← the approved plan
├── .camflow/              ← engine state directory
│   └── (empty; engine creates state.json, trace.log, node-*.json)
└── skills/                ← project-specific skills (if any)
```

### CLAUDE.md template (CAM mode)

```markdown
# <Project name>

<One-paragraph description of the project's purpose.>

## Environment

- Machine: <hostname>
- Scratch: <path>
- Tools:   <the actual versions you checked in Step 2>
- P4 user: <user> (if applicable)

## Key paths

- <describe relevant directories and files>

## What sub-agents should know

- <domain-specific facts the planner can't infer>
- <commands that have non-obvious invocations>
- <constraints: "do NOT modify test_*.py", etc.>

## Stateless execution note

You are a fresh Claude Code agent for each workflow node. Read the
fenced CONTEXT block in your prompt for what previous nodes did.
All context flows through state.json; you have no memory of earlier
nodes in this workflow. Write your result to
.camflow/node-result.json before exiting.
```

### CLI mode additional files

```
<project-dir>/
└── .claude/
    ├── state/
    │   └── workflow.json   ← initial CLI-mode state
    └── skills/
        └── workflow-run/
            └── SKILL.md    ← copy of workflow-run skill if not global
```

Initialize CLI-mode state to the first node:

```json
{"pc": "start", "status": "running"}
```

---

## Step 6: Launch execution

### CLI mode

You stay in the session. The user drives it. Tell them:

```
Setup complete. To run:
  /loop /workflow-run        # loop driver calls workflow-run every tick
or step manually:
  /workflow-run              # run one node at a time

State: <project-dir>/.claude/state/workflow.json
Trace: <project-dir>/.claude/state/trace.log
```

### CAM mode

Launch the engine and then **exit**. The engine runs independently.

```bash
cd <project-dir>
PYTHONPATH=/home/scratch.hren_gpu_1/test/workflow/cam-flow/src \
  nohup python3 -m camflow.cli_entry.main workflow.yaml \
  --node-timeout 3600 \
  --workflow-timeout 28800 \
  --poll-interval 10 \
  > engine.log 2>&1 &
echo "Engine PID: $!"
```

Adjust timeouts for the task at hand:
- Software workflows: `--node-timeout 600` usually fine
- Tree setup / build: `--node-timeout 3600`
- Formal verification: `--node-timeout 7200`

Tell the user:

```
Engine launched (PID <N>). It runs independently — you can close this
session.

Monitor with:
  tail -f <project-dir>/engine.log                     # raw log
  cat  <project-dir>/.camflow/state.json                # current state
  cat  <project-dir>/.camflow/trace.log | tail          # recent nodes

When it finishes, look at:
  cat  <project-dir>/REPORT.md                          # if workflow has a report node
  PYTHONPATH=... python3 -m camflow.cli_entry.main evolve report <project-dir>
```

### Then EXIT

Your job is done. Do NOT stay running to poll state.json. Do NOT
sleep-and-check. The engine does its own thing.

Post-execution reporting is Step 7 — a separate skill invocation
the user makes when they come back.

---

## Step 7: Post-execution report (separate invocation)

When the user returns and asks "how did it go?" or "what happened?",
this step runs as a fresh invocation of the skill — not a
continuation of the earlier setup.

```bash
# Current status
cat <project-dir>/.camflow/state.json | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f'status={s[\"status\"]} pc={s[\"pc\"]} iteration={s[\"iteration\"]}')
print(f'completed: {len(s[\"completed\"])}  failed_approaches: {len(s[\"failed_approaches\"])}')
if s.get('lessons'):
    print('lessons:')
    for l in s['lessons']: print(f'  - {l}')
"

# Trace-based analysis
PYTHONPATH=/home/scratch.hren_gpu_1/test/workflow/cam-flow/src \
  python3 -m camflow.cli_entry.main evolve report <project-dir>

# Final report file, if the workflow wrote one
cat <project-dir>/REPORT.md 2>/dev/null | head -60
```

Summarize to the user: total nodes run, retries, wall-clock time,
which nodes failed (if any), key findings. Offer to dig into
specific nodes from the trace.

---

## Interaction Rules

These are hard rules, not suggestions:

1. **Never skip plan review** (Step 4). Always present the plan and
   get explicit user approval.
2. **Never launch execution** (Step 6) without the user saying "go",
   "approved", or equivalent. Silence is not approval.
3. **When unsure about verify conditions**, ask: "How should I
   verify that `<step>` succeeded?"
4. **When unsure about cmd vs agent**, ask: "Is `<step>` a fixed
   command or does it need AI judgment?"
5. **When domain knowledge is missing**, ask: "What tool / command
   does `<specific thing>` in this project? I don't want to guess."
6. **After launching a CAM engine, EXIT.** Don't stay running.
   Don't poll. Don't sleep-and-check.
7. **Existing workflow.yaml** → ask: "Run it as-is, adapt it, or
   create a new one?"
8. **Do NOT modify RTL or production code** during setup unless the
   user explicitly approved in Step 4 that the `fix` node should do
   so.
9. **Do NOT auto-initialize state.json** if one already exists — ask
   the user whether to resume or restart.
10. **Do NOT run unfamiliar commands.** If the build command for a
    variant isn't in CLAUDE.md, ask the user for the exact
    invocation before writing it into the plan.

---

## Quick reference: paths

- cam-flow repo:         `/home/scratch.hren_gpu_1/test/workflow/cam-flow/`
- Python entry:          `python3 -m camflow.cli_entry.main`
- Planner subcommand:    `python3 -m camflow.cli_entry.main plan "<req>"`
- Evolve subcommand:     `python3 -m camflow.cli_entry.main evolve report <dir>`
- Per-project state:     `<project-dir>/.camflow/state.json`
- Per-project trace:     `<project-dir>/.camflow/trace.log`
- Agent result file:     `<project-dir>/.camflow/node-result.json`

## Quick reference: state schema (CAM mode)

- `pc` — current node
- `status` — running / done / failed / waiting / interrupted
- `iteration` — monotonic step counter
- `completed[]` — history of successful node executions
- `blocked` — current obstacle if any
- `test_output` + `test_history` — latest + summarized prior test runs
- `failed_approaches[]` — what didn't work (cap 5)
- `lessons[]` — durable insights (cap 10, deduped)
- `retry_counts{}` — per-node retry counter
- `current_agent_id` — live camc agent id (for orphan recovery)

## Quick reference: the 6 plan-priority fields per agent node

| Field | Purpose |
|-------|---------|
| `methodology` | Label: rca / simplify-first / search-first / working-backwards / systematic-coverage |
| `escalation_max` | Cap L0..L4 escalation ladder (0–4) |
| `max_retries` | Per-node retry budget (overrides engine default 3) |
| `allowed_tools` | List of Claude Code tools the node is permitted to use |
| `verify` | Shell cmd that runs after success; non-zero exit → status=fail |
| `timeout` | Per-node wall-clock timeout (seconds) |
