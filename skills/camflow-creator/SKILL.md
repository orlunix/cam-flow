---
name: camflow-creator
description: >
  Set up a cam-flow workflow project in either CLI or CAM mode.
  Interactively gathers requirements, investigates the environment,
  generates workflow.yaml via the camflow planner, walks the user
  through the plan for review, and writes the project files. In CAM
  mode launches the engine process and EXITS. In CLI mode writes the
  initial state and tells the user to run camflow-runner via /loop.
  Triggers on "create a workflow", "set up a flow", "new cam-flow
  project", "automate this", "build a pipeline", "/flow", or any
  multi-step task description where the user wants automation.
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  category: orchestration
  tags:
    - workflow
    - automation
    - plan
    - setup
    - cam-flow
    - creator
  maturity: beta
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# camflow-creator

You create cam-flow workflow projects. Two modes:

- **CLI mode**: current Claude session runs nodes itself via
  `/loop camflow-runner`. Minimal YAML. For simple interactive tasks
  (< 5 nodes, no heavy verification).
- **CAM mode**: a Python engine process runs independently and spawns
  fresh camc sub-agents per node. Rich YAML with methodology / verify
  / escalation / tool-scope fields. For autonomous long-running work.

You SET UP the project, then either launch the CAM engine and EXIT,
or hand the CLI flow back to the user. You do not monitor or babysit.
Post-execution reporting is a separate skill invocation the user
triggers later.

## Standing rule

> When uncertain about ANYTHING — missing env info, unclear verify,
> cmd-vs-agent choice, unfamiliar command, ambiguous decomposition —
> **ASK THE USER.** Never guess. Never proceed silently.

---

## Step 0: pick the mode

Choose between CLI and CAM with the user:

- **CLI** — simple, interactive, user stays in the session. Agent
  runs the nodes itself on a `/loop` tick.
- **CAM** — complex, long-running, autonomous. Engine process spawns
  fresh agents. Full plan-priority fields (verify, methodology,
  allowed_tools, max_retries, escalation_max).

**Default: CAM** for anything with loops, verify conditions, or
build/test/deploy steps. CLI is the exception.

If you can't tell from the user's description, ask:

> "This looks like a [simple/complex] task. CLI mode drives it from
> your current session; CAM mode runs it as a background engine with
> full verification. Which do you prefer?"

---

## Step 1: requirements interview

Interview until you have enough to plan. Don't grill on what the
user already answered.

**Must know (both modes):**

1. **Goal.** "all tests pass", "verification report landed",
   "staging deploy completes"
2. **Working directory.** Where does the project live? Suggest a
   path from context.
3. **Steps.** Main stages? Help decompose if the description is
   vague.
4. **Key resources.** Code repos, trees, tools, P4 CLs.

**CAM mode adds:**

5. **Verify conditions.** For each step, how do we KNOW it
   succeeded? Ask explicitly per step.
6. **Failure handling.** Retries? How many?
7. **Tool scope.** Which Claude Code tools each agent needs.

Summarize back to the user every 2–3 answers. Course-correct early.

---

## Step 2: investigate environment

Read the project, don't guess:

```bash
ls <project-dir>/ 2>/dev/null
for t in git python3 pytest ruff mypy smake vcs jg p4 claude camc; do
  which "$t" 2>/dev/null | head -1
done
df -h <project-dir> 2>/dev/null
ls <project-dir>/CLAUDE.md \
   <project-dir>/workflow.yaml \
   <project-dir>/workflow-*.yaml \
   2>/dev/null
p4 info 2>/dev/null | head -5
```

Report findings to the user. Catches missing context early.

If there's already a `workflow.yaml`:

> "Found existing workflow.yaml. Run it as-is, adapt it, or create a
> new one from scratch?"

---

## Step 3: generate the plan

### CLI mode

Write `workflow.yaml` directly. Minimal structure:

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

No plan-priority fields in CLI mode — the user is in the loop and
can intervene on any tick.

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

If the planner fails (no API key, no CLAUDE.md yet, LLM unreachable),
fall back to hand-writing workflow.yaml using these rules:

- Every **agent** node MUST declare: `verify`, `methodology`,
  `escalation_max`, `allowed_tools`, `max_retries`.
- Use **cmd** for deterministic ops (build, test, file check, grep).
- Use **agent** for creative / analytical work.
- Methodology picks:
  - `simplify-first` — env setup, build, deploy, report
  - `search-first` — research, code analysis, reading RTL
  - `rca` — debugging, fixing failures
  - `working-backwards` — design, planning, verification plans
  - `systematic-coverage` — running tests, executing verification
- Allowed-tool recipes:
  - fix: `[Read, Edit, Write, Bash]`
  - analysis: `[Read, Glob, Grep, Bash]`
  - planning: `[Read, Write]`
- Verify cookbook:
  - `test -f <path>` (file created)
  - `test -s <path>` (file non-empty)
  - `test -n "{{state.key}}"` (state value set)
  - the test command itself (tests pass)
  - `grep -q "<pat>" <file>` (code landed)

---

## Step 4: review with the user (MANDATORY)

Present the plan structured:

```
=== Workflow plan (<CLI|CAM> mode) ===

  setup → build → find-cl → analyze → plan-verify →
  run-verify → report → done
                           ↑         │fail
                           └── fix ◄─┘

Nodes: 9 (6 agent, 2 cmd, 1 done)
Estimated time: ~60 min

Node details:
  1. setup-tree     agent   simplify-first     max_retries=2
     Verify: test -d <tree>/hw/nvip/ip/peregrine/5.1/vmod
     Tools:  [Read, Bash, Write]

  ... (one block per node) ...
```

Ask explicit questions per agent node:

- "Is the verify condition for `<node>` correct?"
- "Should `<node>` be cmd instead of agent?"
- "Any domain knowledge missing from CLAUDE.md that `<node>` needs?"

If the user changes anything, **regenerate and re-show.** Do NOT
apply edits silently.

Do NOT proceed past this step without explicit "go", "approved", or
equivalent.

---

## Step 5: write project files

```
<project-dir>/
├── CLAUDE.md              ← domain knowledge for sub-agents
├── workflow.yaml          ← the approved plan
├── .camflow/              ← CAM mode engine state dir (empty now)
├── .claude/               ← CLI mode only
│   └── state/
│       └── workflow.json  ← initial CLI-mode state
└── skills/                ← project-specific skills (if any)
```

### CLAUDE.md

Include:

- Project purpose (one paragraph)
- Environment (hostname, scratch path, tool versions, P4 user)
- Key paths (directory layout)
- Domain-specific knowledge the user provided
- Non-obvious command invocations
- Constraints ("do NOT modify test_*.py", etc.)
- CAM-mode stateless note: "You are a fresh agent for each node.
  Read the CONTEXT block in your prompt. Write results to
  `.camflow/node-result.json`."
- CLI-mode note: "State lives at `.claude/state/workflow.json`.
  The `/loop camflow-runner` driver calls you once per tick."

### CLI-mode state seed

Initialize `.claude/state/workflow.json` to the first node:

```json
{"pc": "start", "status": "running"}
```

---

## Step 6: launch

### CLI mode

Tell the user:

```
Setup complete.

To run (single-step):
  /camflow-runner

To auto-advance (loop every minute):
  /loop camflow-runner

State lives at .claude/state/workflow.json
Trace lives at .claude/state/trace.log

You stay in this session; camflow-runner handles each tick.
```

Do NOT launch `/loop` yourself. The user decides when to start.

### CAM mode

Launch the engine in the background, print the PID, and EXIT:

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

Adjust timeouts for the task:
- Software workflows: 600 s node / 1 h workflow
- Tree setup / build: 3600 s node / 8 h workflow
- Formal verification: 7200 s node / 12 h workflow

Tell the user:

```
Engine launched (PID <N>). Runs independently — you can close this
session.

Monitor:
  tail -f <project-dir>/engine.log
  cat  <project-dir>/.camflow/state.json
  cat  <project-dir>/.camflow/trace.log | tail

When done:
  cat <project-dir>/REPORT.md
  PYTHONPATH=... python3 -m camflow.cli_entry.main evolve report <project-dir>
```

**Then EXIT.** Your job is done.

---

## Interaction rules

1. **Never skip plan review** (Step 4). No exceptions.
2. **Never launch** (Step 6) without explicit user approval.
3. **When unsure about verify**, ASK: "How should I verify
   `<step>`?"
4. **When unsure about cmd vs agent**, ASK.
5. **When domain knowledge is missing**, ASK. Don't guess.
6. **After launching CAM engine, EXIT.** Don't poll.
7. **Existing workflow.yaml** → ASK: run as-is / adapt / new.
8. **Do NOT modify RTL or production code** unless Step 4 approval
   specifically includes it.
9. **Do NOT auto-initialize state.json over existing state** — ASK.
10. **Do NOT run unfamiliar commands.** ASK the user for the exact
    invocation.

---

## What this skill does NOT do

- Does not monitor the engine (use a separate invocation for
  post-execution reporting)
- Does not run nodes itself in CLI mode — that's camflow-runner
- Does not modify RTL code without user sign-off
- Does not auto-approve L4 escalations
- Does not skip the plan review phase

## Companion skills

- `camflow-runner` — CLI-mode per-tick executor. The user drives it
  via `/loop camflow-runner` after camflow-creator finishes Step 6.
- `camflow plan` CLI — the single-call planner used in Step 3.
- `camflow evolve report` CLI — post-execution trace analysis.

## Quick reference

- cam-flow repo:         `/home/scratch.hren_gpu_1/test/workflow/cam-flow/`
- Python entry:          `python3 -m camflow.cli_entry.main`
- CAM state:             `<project>/.camflow/state.json`
- CAM trace:             `<project>/.camflow/trace.log`
- CLI state:             `<project>/.claude/state/workflow.json`
- CLI trace:             `<project>/.claude/state/trace.log`
- Agent result file:     `<project>/.camflow/node-result.json`
