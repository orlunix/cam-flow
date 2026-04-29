# camflow Harness Design — Thin Edition

**Status**: Design under simplification (2026-04-28). Replaces the
Phase A / B / C ambition captured in
`archive/design-next-phase.md`.

**Audience**: anyone trying to understand camflow's *real* shape after
we agreed to subtract.

**One-line summary**: a thin Python **Workflow Engine** dispatches
nodes deterministically; three LLM roles (Planner, Worker, Evaluator)
each follow a prompt template; one LLM **Orchestrator** sits OUT of
the dispatch path and is woken only by (a) Monitor-detected failures
or (b) human chat. Total target: ~4,000 lines of code (down from
15,000), driven by hard subtraction of the Steward action machinery
(autonomy / confirm / mutating ctl / compaction handoff / extended
events / private dirs) that we built but didn't need.

---

## 1. Why this doc exists

We wrote ~15,000 lines of Python + ~12,700 lines of tests for a system
that — measured against what it actually has to do — could be ~1,500
lines. That bloat came from one root cause: **we did not trust the
LLMs to follow their prompts**, so we re-implemented their job in
deterministic Python (Engine, transition resolver, state_enricher,
ctl mutating verbs, autonomy gates, confirm flow, compaction handoff,
extended events, registry, etc.).

This doc states what we DO trust and what we DON'T. The boundary is
deliberately drawn so the harness is small.

---

## 2. Architecture — Engine vs Orchestrator are different layers

The **Workflow Engine is the dispatcher**. It's deterministic Python.
It does the boring work: read state, spawn next Worker, poll result,
call Evaluator, advance state, repeat.

The **Orchestrator is an LLM advisor that sits OUT of the dispatch
path**. It's idle by default — zero CPU, zero token cost. It's woken
only by:

  (a) the **Monitor** noticing the workflow stopped / exited /
      errored / hung, OR
  (b) a **human message** via `camflow chat`.

When woken, the Orchestrator inspects state, decides what to do, and
takes action by **invoking ordinary CLI commands** (`camflow resume`,
`camflow ctl read-*`, `camflow stop`, etc.) — no custom verb
whitelist, no autonomy config, no confirm queue. It's a normal
Claude Code agent with `Read` + `Bash` + `Write` (only its own memory
files); the prompt rules are the safety boundary.

```
   user NL request
       │
       ▼
   ┌──────────────┐
   │   Planner    │  one-shot LLM
   │              │  writes dag.json AND per-node prompts
   └──────┬───────┘  (specialises prompts/worker.md → .camflow/nodes/N.prompt)
          │
          ▼
   ╔══════════════════════════════════════════════════════════════╗
   ║   Workflow Engine (Python, deterministic)                     ║
   ║                                                                ║
   ║   for each pc in dag.json:                                     ║
   ║     spawn Worker reading .camflow/nodes/<pc>.prompt            ║
   ║     poll .camflow/results/<pc>.json                            ║
   ║     spawn Evaluator (or run shell verify) → Pass / Fail        ║
   ║     advance state.json per next_node_pass / next_node_fail     ║
   ║     on N-th failure: emit event, halt                          ║
   ╚════════╤═══════════════════════════════════════════╤═══════════╝
            │ events / heartbeat                          │ workers
            ▼                                            ▼
   ┌──────────────┐                                ┌──────────────┐
   │   Monitor    │                                │   Worker     │  per-node
   │  (cron job   │                                │    (LLM)     │  one-shot
   │  OR engine   │                                │              │  reads its
   │  self-check  │                                └──────┬───────┘  N.prompt
   │  + watchdog) │                                       │
   └──────┬───────┘                                       ▼
          │ "workflow stuck/dead/errored,                  ┌──────────┐
          │  please debug + resume"                        │Evaluator │  per-node
          ▼                                                │  (LLM)   │  one-shot
   ┌─────────────────────────────────────────────────┐    └──────────┘
   │   Orchestrator (LLM, project-scoped, IDLE       │
   │                  by default)                     │
   │                                                  │
   │   Wakes on:                                      │
   │     - Monitor notification (function 1)          │
   │     - camflow chat <msg>     (function 2)        │
   │                                                  │
   │   Tools: Read, Write (own memory), Bash          │
   │   Actions: invoke camflow CLI (resume / stop /   │
   │            ctl read-* / status / plan replan),   │
   │            edit dag.json carefully if needed,    │
   │            answer the human                      │
   └─────────────────────────────────────────────────┘
            ▲
            │ camflow chat "<msg>"
            │
          Human
```

### 2.1 Roles in detail

**Planner** — LLM, runs **once** at the start of a flow.
- Input: user's NL request + project context (CLAUDE.md, skills/).
- Output: `dag.json` (workflow graph) AND a `.camflow/nodes/<N>.prompt`
  file for every node, each one a fully-formed runnable prompt
  produced by **specialising the master `prompts/worker.md` template**
  with that node's task, inputs, outputs, and acceptance criteria.
- Dies after writing.
- Validation: runs `camflow plan-tool validate dag.json` (DSL +
  plan-quality) before exiting. Re-tries on failure inside its own
  loop.

**Workflow Engine** — Python, deterministic, runs throughout the flow.
- Reads `dag.json` once at start, `state.json` every iteration.
- Per iteration: spawn the Worker for `state.pc`, poll its
  `result.json`, run the Evaluator (LLM agent OR shell `verify` cmd
  per node config), advance `state.pc` per `next_node_pass` /
  `next_node_fail`, retry up to `max_retry` on Fail.
- Atomic state writes via temp+rename+fsync.
- Holds the project lock.
- Emits events to the Monitor on terminal states (success / fail /
  hung / orphaned).
- This is what "Engine" already is in our codebase. Trim, don't
  rewrite.

**Worker** — LLM, **one-shot per node**.
- Reads `.camflow/nodes/<N>.prompt` (Planner produced).
- Executes the task.
- Writes `.camflow/results/<N>.json` and exits.

**Evaluator** — LLM **or** shell command, runs **after each Worker**.
- Two flavours per node, picked by the workflow author:
  - **Shell**: a `verify` cmd in `dag.json` (existing today). Cheap,
    deterministic, exit-0 = Pass. Best for "did the test pass?".
  - **LLM**: spawn an Evaluator agent reading
    `prompts/evaluator.md` + the Worker's `result.json` +
    `acceptance_criteria`. Best for "is this output correct?".
- Outputs strictly `Pass` / `Fail` + a one-line reason. No business
  logic, no rewriting.

**Orchestrator** — LLM, **project-scoped, idle by default, woken on
event**.
- Two functions only (Huailu's spec):
  1. **Exception handler** — Monitor sends "workflow stopped /
     errored / hung at node X". Orchestrator reads `state.json` +
     `trace.log`, debugs, decides: resume / replan / fail. Calls
     `camflow resume` (or whatever CLI is right) via Bash.
  2. **Human-in-the-loop** — `camflow chat "<msg>"` arrives.
     Orchestrator interprets, takes any action via CLI / file edits.
     **Open verb set** — not a whitelist. The prompt rules are the
     safety boundary.
- Does NOT dispatch workers.
- Does NOT advance state.
- Does NOT auto-act outside the two trigger conditions.
- Idle = zero cost.

### 2.1 Roles in detail

**Planner** — LLM, runs **once** at the start of a flow.
- Input: user's NL request + project context (CLAUDE.md, skills/).
- Output: `dag.json` (workflow graph) AND a `.camflow/nodes/<N>.prompt`
  file for every node, each one a fully-formed runnable prompt
  produced by **specialising the master `prompts/worker.md` template**
  with that node's task, inputs, outputs, and acceptance criteria.
- Dies after writing.
- Validation: runs `camflow plan-tool validate dag.json` (DSL +
  plan-quality) before exiting. Re-tries on failure inside its own
  loop.

**Orchestrator** — LLM, **resident throughout the flow**.
- Reads `state.json` and `dag.json` at the start of every iteration.
- Decides what node runs next per the DAG's `next_node_pass` /
  `next_node_fail` rules — it does not invent steps.
- Spawns the Worker for that node, passing the path to
  `.camflow/nodes/<N>.prompt`.
- Polls for the Worker's `result.json`.
- Spawns the Evaluator with the result.
- Updates `state.json` based on Pass/Fail.
- Three additional jobs (Huailu's spec):
  1. **Exception handler** — when a node fails, the Orchestrator
     decides retry / replan / fail, calls `camflow resume`, etc.
  2. **Receive human request** — `camflow chat "<msg>"` arrives as a
     message to the Orchestrator agent; it interprets and acts. Open
     verb set; the Orchestrator may invoke any tool it has to.
  3. **Summarise / report** — at flow end, writes a final report and
     archives memory.

**Evaluator** — LLM, runs **after each Worker** completes.
- Reads the node's `acceptance_criteria` from `dag.json` and the
  Worker's `result.json`.
- Outputs strictly `Pass` / `Fail` + a one-line reason. No business
  logic, no rewriting.

**Worker** — LLM, **one-shot per node**.
- Reads its specific `.camflow/nodes/<N>.prompt` (Planner produced).
- Executes the task.
- Writes `.camflow/nodes/<N>/result.json` and exits.

### 2.2 Why Engine is Python and Orchestrator is LLM

This is the one architectural call that matters most:

- **Engine = Python (deterministic)**. The dispatch loop is small,
  tight, and well-understood: read dag, read state, spawn next, poll
  result, advance. This was built and validated months ago. Making it
  an LLM would mean every tick is a $0.05 LLM call AND a
  non-deterministic decision about "what's next?". The prompt would
  have to re-state what `next_node_pass` already says in JSON.
  Wrong trade.

- **Orchestrator = LLM (advisor, event-driven)**. Exception debugging
  and human conversation are exactly what LLMs are good at. They're
  also infrequent — most of the time the Orchestrator is sleeping. A
  Python "exception handler" can't read `trace.log`, infer that the
  failure pattern matches "the test has a flaky retry loop", and
  decide to call `camflow resume --skip-node verify`. An LLM can.

- **Worker = LLM**. Obvious — the actual work is LLM-native.

- **Evaluator = LLM OR shell**. Shell when the criterion is binary
  ("did pytest pass?"); LLM when it's qualitative ("does the README
  cover the new feature?").

- **Monitor = trivial Python**. Polls heartbeat, sends a `camc send
  <orchestrator-id> "<event-json>"` when something looks wrong. ~50
  lines.

The deterministic harness owns: spawn / poll / state write / lock /
watchdog / Monitor. Total ~3,000 lines. The Orchestrator agent owns
nothing — it has Read/Bash/Write-own-memory and uses normal CLI to
act. Total Python: ~3,500–4,000 lines including current
Engine + Watchdog + dsl + transition + state_enricher.

---

## 3. The 2 Orchestrator functions (Huailu's spec)

| # | Function | Trigger | Action |
|---|----------|---------|--------|
| 1 | **Exception handler** | Monitor (cron job OR engine self-check OR watchdog) detects: workflow stopped / exited / errored / hung. Sends a notification message to the Orchestrator. | Orchestrator wakes, reads `state.json` + `trace.log`, debugs, decides resume strategy. Calls `camflow resume` (or `replan`, or whatever) via Bash. |
| 2 | **Human-in-the-loop** | `camflow chat "<msg>"` → message arrives at Orchestrator agent | Orchestrator interprets the request and takes any action — invoking CLI commands, editing files, answering. **Open action surface, not a verb whitelist.** |

That's the entire job description. Orchestrator does NOT:
- Dispatch workers (Workflow Engine does).
- Advance state.json (Workflow Engine does).
- Run on every tick (idle by default; woken only on the two triggers).
- Have a custom verb API to invoke (just uses Bash + the existing
  `camflow` CLI like a human would).

**Implication for tools**: it has whatever `camc run` gives a Claude
Code agent — Read, Write, Bash, Grep. The prompt + CLAUDE.md rules
are the safety boundary. No autonomy levels, no confirm queue, no
mutating-verb whitelist. **All of those layers go away.**

---

## 4. Filesystem layout

```
project_dir/
├── CLAUDE.md                       # global rules — what every agent must respect
├── prompts/                         # source of truth, in repo
│   ├── planner.md
│   ├── orchestrator.md
│   ├── evaluator.md
│   └── worker.md                   # MASTER template; Planner specialises per-node
├── camflow                         # ~150-line bash driver
└── .camflow/
    ├── dag.json                    # written by Planner
    ├── state.json                  # written by Orchestrator (atomic + locked)
    ├── trace.log                   # all agents append (one JSONL per event)
    ├── orchestrator.lock           # held by the live Orchestrator
    ├── orchestrator.heartbeat      # for watchdog
    ├── nodes/
    │   ├── N01.prompt              # Planner specialised worker.md → here
    │   ├── N02.prompt
    │   └── ...
    ├── results/
    │   ├── N01.json                # Worker writes
    │   └── N02.json
    ├── eval/
    │   ├── N01.json                # Evaluator writes Pass/Fail + reason
    │   └── N02.json
    └── report.md                   # Orchestrator writes at end
```

**No** `agents.json` registry, **no** per-attempt directory tree,
**no** `steward/` directory, **no** `flows/<flow_id>/` namespace,
**no** `control.jsonl` queue, **no** `steward-config.yaml`. The
prompts and `state.json` carry everything.

If a node retries, the Worker just re-runs and overwrites
`results/N01.json`. If you want history, `trace.log` has it.

---

## 5. The deterministic Python / bash glue

Total target: ~600 lines.

| Component | Lines | Purpose |
|-----------|------:|---------|
| `camflow` (bash) | ~150 | CLI driver — `plan` / `run` / `resume` / `chat` / `status` / `stop`. Spawns the right agent with the right prompt; handles the watchdog loop. |
| `camflow_state.py` | ~150 | `load_state` / `save_state_atomic` (temp+rename+fsync), schema check, file lock. |
| `camflow_agent.py` | ~200 | Wraps `camc run` / `camc send` / `camc capture` / `camc rm`. Polls for result file. |
| `camflow_watchdog.py` | ~100 | Heartbeat-based restart of the Orchestrator. Reuses watchdog era code. |

That's the entire deterministic surface. Compared to 15k lines today,
this is ~25× smaller.

---

## 6. What we explicitly DROP from current camflow

We are NOT dropping the Workflow Engine — it stays as the
deterministic dispatcher. We ARE dropping every layer that gave
"Steward" the ability to take action through a custom verb whitelist
and the supporting machinery for that.

| Dropped | Why it's not needed |
|---------|---------------------|
| Mutating ctl verbs (pause / resume / kill-worker / spawn / skip / replan) | Orchestrator invokes `camflow resume`, `camc rm <id>`, edits `dag.json`, etc. via Bash. No need for a verb API; CLI is the API. |
| `cli_entry/ctl_mutate.py` + `cli_entry/ctl_steward.py` (summarize, archive-summary) | Orchestrator writes its own `summary.md` / `archive.md` via the Write tool. |
| `backend/cam/control_drain.py` (engine drains control queue) | Orchestrator doesn't queue commands; it just runs CLI directly. No drain queue. |
| Autonomy config (`steward-config.yaml`, `autonomy.py`) + presets (cautious/default/bold) | No verb whitelist → no autonomy gating needed. Prompt rules are the boundary. |
| Confirm flow (`control-pending.jsonl`, `chat --pending`, `[y/N/never]`) | Same — no queue to confirm. Human asks Orchestrator directly via `camflow chat`. |
| Compaction handoff (`steward/handoff.py`, summarize/archive verbs as triggered actions, archive subdir tree) | If Orchestrator hits the context wall, watchdog `camc rm`s it and a fresh one is spawned next time it's needed. State is in `state.json` + `trace.log`; no memory carryover required. |
| Extended event set wrappers (`node_retry`, `escalation_level_change`, `verify_failed`, `heartbeat_stale_worker`, `checkpoint_now`, `flow_idle`) + their engine wiring | Engine emits one generic `node_failed` / `flow_terminal` event when something terminal happens. Monitor / Orchestrator decide what to do. No need for 6 different event types. |
| `agents.json` registry + `registry/` package | `state.json` carries `current_agent_id`; `trace.log` carries history. Two files instead of three. |
| Per-attempt private directories (`flows/<flow>/nodes/<n>/attempts/<n>/...`) and `paths.py` helpers | `.camflow/results/<node>.json` overwrites on retry; trace.log carries the history. Simpler hierarchy. |
| trace.log tagged-union (15 reserved kinds, of which most are unwritten) | trace.log is plain append-only JSONL with `{ts, actor, kind, fields}`. `kind` is free-form, not a closed enum. |
| Smooth-mode countdown handles (`e<enter>` edit, `r<enter>` replan) + the loop | `camflow plan` writes yaml; user inspects; `camflow run` starts. Two commands, no countdown. |
| `plan -i` interactive Planner mode | `camc capture <planner-id>` + `camc send <planner-id> <text>` gives the same UX without bespoke code. |
| `--no-steward` flag | Orchestrator is so cheap (idle = zero cost) that there's no reason to opt out. Drop the flag. |
| `cli_entry/smooth.py` (the 7-step driver) | `camflow plan` + `camflow run` is enough. If you want one-liner UX, write a 5-line shell alias. |
| Agent-based Planner (`planner/agent_planner.py`) | The pre-Phase-A single-shot LLM Planner is 80% as good for 5% of the tokens. Keep it; drop the agent path. |
| `_block_real_camc` autouse fixture chains, multi-conftest test-isolation infra | With the simpler architecture, fewer integration points need defensive mocking. |
| Steward boot pack regen / private dir / inbox / etc. | Orchestrator runs as a normal `camc` agent reading `prompts/orchestrator.md` + `CLAUDE.md`. No special bootstrapping. |

---

## 7. What stays (the harness — the deterministic core)

| Kept | Lines (rough) | Why |
|------|--------------:|-----|
| **Workflow Engine** (`backend/cam/engine.py`, trimmed) | ~700 | Deterministic dispatcher: read state, spawn next, advance per dag rules. |
| **Worker spawn** (`backend/cam/agent_runner.py`, simplified) | ~500 | `camc run` + result polling. Subtle and well-tested; don't rewrite. |
| **DSL + validator** (`engine/dsl.py`) | ~400 | Defines `dag.json` grammar; validates before Engine trusts it. |
| **Transition resolver** (`engine/transition.py`) | ~150 | next_node_pass / next_node_fail logic. Small but load-bearing. |
| **State enricher** (`engine/state_enricher.py`, trimmed) | ~200 | Merges Worker result into state. Less six-section magic, more direct. |
| **Atomic state write** (`backend/persistence.py`) | ~150 | Crash-safe state is non-negotiable. |
| **Watchdog + lock + heartbeat** (`engine/monitor.py`) | ~500 | Engine might die; watchdog restarts. Lock = one Engine at a time. |
| **trace.log append** (`backend/cam/tracer.py`, simplified) | ~80 | Plain append-only JSONL with `{ts, actor, kind, fields}`. No closed kind set. |
| **Planner** (`planner/planner.py`, legacy single-shot) | ~400 | One LLM call → workflow yaml. Keep. Drop the agent_planner alternative. |
| **Plan-tool validate/write** (`cli_entry/plan_tool.py`) | ~200 | Planner's self-validation hook. Used at plan time. |
| **CLI entry** (`cli_entry/main.py` + run/resume/status/stop/watchdog/plan/chat) | ~800 | The 7 user-facing subcommands. Most code already exists; just drop the Steward subcommands. |
| **Monitor** (NEW, ~50 lines or fold into watchdog) | ~50 | Detects "stuck/dead/errored", sends `camc send <orch-id> "<event>"`. |
| **Total Python harness** | **~4,100** | (vs current 15,000) |

Plus 4 prompt files + CLAUDE.md (zero Python). The deterministic core
is mostly already written and battle-tested; we're trimming Steward
support code, not building anew.

---

## 8. The user-facing CLI (target)

```bash
camflow plan "build a calculator with tests"
   # → spawns Planner LLM → produces dag.json + nodes/*.prompt
   # → Planner exits after writing

camflow run
   # → starts Workflow Engine (Python deterministic) in foreground
   #   or daemon. Engine spawns Workers per node, runs Evaluator,
   #   advances state.
   # → also ensures the project's Orchestrator is alive (spawn if
   #   not). Engine emits events to it, but never depends on it.

camflow resume
   # → same as run, but doesn't wipe state.json first.

camflow chat "<msg>"
   # → camc send <orchestrator-id> "<msg>"
   # → Orchestrator wakes, interprets, acts via Bash + camflow CLI.

camflow status
   # → pretty-print state.json + heartbeat + which agent is alive.

camflow stop
   # → SIGTERM the Engine; release lock.

camflow watchdog
   # → background loop: poll heartbeat. If Engine dies, restart it
   #   AND notify the Orchestrator.
```

**7 subcommands.** Current camflow has 14+ — `chat` / `steward` /
`steward kill | restart | status` / `ctl read-* / pause / resume /
kill-worker / spawn / skip / replan / summarize / archive-summary` /
`plan-tool validate | write` / `smooth` / `plan -i`. We collapse the
mutating ctl verbs back into the CLI commands the Orchestrator
already knows how to call (`camflow resume`, `camflow stop`, `camc rm
<id>`, `cat .camflow/state.json`).

---

## 9. CLAUDE.md — the global rules

Lives at the project root. Every agent reads it before doing anything.
Captures the contract that is currently spread across 15k lines of
Python:

```markdown
# Global Harness Rules

## 1. State discipline
- Only the Orchestrator writes state.json. Everyone else reads.
- Always read state.json BEFORE doing anything; write atomically AFTER.
- state.workflow_status is the only signal of "done". Don't infer.

## 2. Role boundaries
- Planner: runs once, writes dag.json + nodes/*.prompt, exits.
- Orchestrator: dispatches Workers, calls Evaluator, advances state.
- Evaluator: Pass/Fail only. No business logic.
- Worker: do your one task per .camflow/nodes/<N>.prompt; write results/<N>.json; exit.

## 3. Failure handling
- Worker may retry up to dag.max_retry per node.
- Beyond that, Orchestrator decides: replan / fail / ask the user.

## 4. Append to trace.log on every meaningful action
- {ts, actor, kind, fields} JSONL.

## 5. English only.
## 6. Never write outside .camflow/ unless the workflow says so.
```

---

## 10. Open questions for Huailu

These aren't answered by this doc; they need a decision before the
code reset:

1. **Archive branch name**: I'd suggest `archive/phase-abc-2026-04`.
2. **Reset point**: `3b0f1f5` (post-watchdog ship, pre-Phase-A). This
   is where the Workflow Engine + Watchdog + DSL + Planner already
   work and Steward hasn't been added yet. The cleanest restart
   point.
3. **Derive vs fresh-write**: derive from `3b0f1f5`. The kept modules
   (engine, agent_runner, monitor, persistence, tracer, dsl) are all
   battle-tested at that commit.
4. ✅ **RESOLVED** — Engine = Python (deterministic dispatcher),
   Orchestrator = LLM (event-driven advisor). Different layers, not
   the same role. Harness target is ~4,000 lines, not 1,500.
5. **Evaluator: per-node LLM agent or shell `verify` cmd, or both?**
   Recommend: workflow author picks per node. `dag.json` schema
   carries either `verify: <shell-cmd>` or `evaluator: agent` for
   each node. Default `verify: true` (no Evaluator). When `evaluator:
   agent`, Engine spawns an Evaluator using `prompts/evaluator.md`.
6. **DAG format**: keep current yaml DSL? Convert to doubao-style
   `dag.json`? Yaml is what we have and what the existing legacy
   Planner produces. **Recommend keep yaml**; conversion has cost
   for no real benefit. The shape (nodes / next / acceptance / retry
   / etc.) is identical either way.
7. **Monitor**: separate component or fold into watchdog?
   Recommend: extend the current watchdog to also notify Orchestrator
   on death/stale events. Adds ~50 lines to the existing
   `monitor.py`. No new module needed.

---

## 11. Migration plan (gist)

1. Get this doc's open questions answered.
2. Update `strategy.md` / `architecture.md` / `roadmap.md` to point
   here.
3. Move the historical docs (`design-next-phase.md`,
   `triage-2026-04-26.md`, etc.) to `docs/archive/`.
4. Branch the current 24 commits as `archive/phase-abc-2026-04`.
5. Reset `main` to the chosen reset point.
6. Build the thin harness from there: 4 prompts + bash driver + the
   small Python glue.
7. Validate with one real flow end-to-end before declaring v1.

Code work doesn't start until step 6. Steps 1-5 are docs + git.

---

## 12. What this design does NOT prevent

- Adding a feature later that **does** require Python (e.g., true
  per-tick worker liveness probe, distributed flows). Add it when
  you hit the actual problem.
- Re-introducing pieces we cut (e.g., a chat history viewer if the
  Orchestrator's session log isn't enough). We have them on the
  archive branch; cherry-pick when needed.
- Disagreeing with the all-LLM-Orchestrator call. Open question 4
  exists for a reason.

The principle is: **start thin, grow on demand**, not the reverse.
