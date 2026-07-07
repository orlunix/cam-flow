# Next-Phase CamFlow Design — Steward + Smooth Mode + Trust Model

> **Historical v1.1 proposal.** Smooth mode, a persistent Steward, and mandatory/default Planner orchestration are outside the v1.2 runner design. This document remains for rationale only; see [`design-v1.2.md`](design-v1.2.md) for the current specification.


**Status**: Phase 1 design spec — final revision before implementation.
**Author**: camflow-dev (claude opus 4.7)
**Date**: 2026-04-25, last revised 2026-04-26
**Source brief**: [`next-design-discussion.md`](next-design-discussion.md)
**Tracks**: [`strategy.md`](strategy.md), [`architecture.md`](architecture.md),
[`roadmap.md`](roadmap.md), [`self-monitoring.md`](self-monitoring.md).

---

## Revision history

- **v1 (2026-04-25)** — Initial spec. Father agent per flow, single-call
  Planner. Many gaps left as open questions.
- **v2 (2026-04-25)** — Planner upgraded to a self-sufficient agent.
  Manager skill compressed from 8 phases to 3.
- **v3 (2026-04-26)** — Father renamed to **Steward**. Steward made
  project-scoped (one per `.camflow/`), never auto-exits. Added
  `.camflow/agents.json` registry and unified trace.log event schema.
  Formalized **trust model** (LLM advisory, deterministic dispatch).
  Added **autonomy configuration** with confirm-on-risky-verbs.

---

## 1. TL;DR

Four interlocking changes:

1. **Trust model formalized.** Engine and Watchdog stay deterministic
   Python — the only authoritative dispatchers / state writers. Planner
   and Steward are LLM agents whose outputs always pass through
   deterministic validators before taking effect. This is the spine
   that makes everything else safe.
2. **Planner becomes a self-sufficient agent** — drops thin NL on it,
   it explores the project, self-critiques, writes a validated yaml.
3. **Steward** (renamed from Father) — one persistent project-scoped
   LLM agent. Born with the first flow in a project, lives across all
   subsequent flows, **never auto-exits**. It is the user's primary
   interface, the project's long-term memory, and a configurable-
   autonomy advisor — but never a dispatcher.
4. **Frictionless entry point** `camflow "<task>"` — smooth mode end
   to end without writing yaml.

Engine and watchdog stay exactly as today. Steward and the upgraded
Planner are additive — `--no-steward` and existing `camflow plan`
keep current behavior available.

---

## 2. Goals and non-goals

### Goals

1. `camflow "<request>"` works as a one-liner.
2. Planner self-validates and explores; quality is the Planner's own
   responsibility, not something Manager has to scaffold.
3. Every project (`.camflow/` directory) gets one persistent Steward
   the user can query in natural language.
4. Steward can take corrective action through a small, deterministic
   verb set whose risky verbs require user confirmation by default.
5. Multiple projects run concurrently without sharing state or
   context (one Steward per project).
6. Today's behaviors continue to work unchanged: `camflow run <yaml>`,
   watchdog, lock self-heal, resume after crash, deterministic
   scheduling.

### Non-goals (Phase 1)

- **Replacing the engine with an LLM scheduler.** Engine is the only
  dispatcher and the only writer of `state.json`. Steward proposes;
  Engine disposes.
- Cross-flow / cross-project coordination ("do these 3 flows in
  lock-step"). Multiple Stewards stay isolated.
- Steward editing `workflow.yaml` in place. Plan changes go through
  Planner re-invocation (`camflow ctl replan`).
- Multi-machine Steward (Steward lives on the same host as its
  engine). Cross-host orchestration is a `cam` problem.
- Trace.log rotation (deferred — see §13.5).

---

## 3. Trust model — the spine

The central design tension: we want **(a) deterministic control** for
scheduling and state, **(b) a main agent** the user can talk to and
trust to remember things, but **(c) we don't trust an LLM to dispatch
work**. The resolution: separate **intelligence** from **execution**.

### 3.1 Layered trust

```
                          Implementation         Failure containment
   ┌─────────────────────────────────────────────────────────────┐
   │ Engine          │  deterministic Python  │ code review, unit   │
   │                 │                        │ tests, atomic state │
   │ Watchdog        │  deterministic Python  │ same                │
   ├─────────────────────────────────────────────────────────────┤
   │ Planner agent   │  LLM                   │ validate_workflow   │
   │  (writes yaml)  │                        │ tool + DSL check +  │
   │                 │                        │ countdown veto      │
   ├─────────────────────────────────────────────────────────────┤
   │ Steward         │  LLM                   │ ctl verb whitelist  │
   │  (proposes)     │                        │ + autonomy config + │
   │                 │                        │ engine validates    │
   │                 │                        │ + risky verbs       │
   │                 │                        │ require user        │
   │                 │                        │ confirm             │
   └─────────────────────────────────────────────────────────────┘
```

**LLMs are never on the critical path.** Every LLM output is filtered
through a deterministic validator/dispatcher before it can change
system state.

- Planner produces a yaml → `validate_workflow` (DSL + plan-quality)
  must pass → user countdown gives 5 s veto.
- Steward emits a control command → `camflow ctl` checks the verb
  against the whitelist + arg schema → for "risky" verbs, queues to
  pending/confirm queue → engine drains only confirmed queue.
- Workers produce `node-result.json` → `result_reader` validates →
  malformed result synthesizes a typed fail.

### 3.2 What "main agent" means

"Steward is the main agent" reads as:

- ✅ **Interface**: any question about this project, you talk to Steward.
- ✅ **Memory**: Steward is the persistent project memory across flows.
- ✅ **Diagnosis**: Steward interprets events, explains failures.
- ✅ **Proposal**: Steward proposes corrective actions.

It does NOT read as:

- ❌ Steward decides which node runs next (Engine does, per yaml).
- ❌ Steward writes `state.json` (Engine is sole writer).
- ❌ Steward controls locks (Engine + Watchdog).
- ❌ Steward acts unilaterally on risky operations (gated by autonomy).

Analogy: a ship's captain commands and remembers and decides, but
doesn't personally turn the rudder or stoke the boilers. The captain
is unmistakably the brain. The mechanical systems are unmistakably
authoritative for execution.

### 3.3 Why this resolves the tension

- The user gets a **deterministic backbone** they can audit and
  unit-test (Engine + Watchdog unchanged).
- The user gets a **single conversational interface** with
  cross-flow memory (Steward as the front desk).
- The user **doesn't have to trust an LLM with dispatch authority** —
  Steward's autonomy is dialed via configuration, and risky
  operations require confirmation.

---

## 4. Architectural prerequisite — Planner as agent

This is a foundational implementation change that everything else
depends on.

### 4.1 Why

Single-shot Planner outsources its quality to scouts and to the
Manager skill's GATHER phase. When input is thin, output is thin. A
**good plan agent should guarantee its own quality** — self-explore
the codebase, self-critique its draft, self-validate against the DSL.

### 4.2 Shape

Planner is a camc-spawned agent named `planner-<flow-shortid>`,
restricted tool set:

| Tool                  | Purpose                                       |
|-----------------------|-----------------------------------------------|
| Read / Glob / Grep    | Explore project, CLAUDE.md, skills/, agents/  |
| Bash (allowlisted)    | `which`, `--version`, `ls`, `git log` env probes |
| skillm search         | Skill catalog (replaces today's scout)         |
| `validate_workflow`   | Custom tool: returns DSL + plan-quality errors |
| `write_yaml`          | Custom tool: atomic write to target path       |

### 4.3 Internal critique loop

```
1. Understand request — read CLAUDE.md, glance at skills/, agents/
2. Draft yaml — pick methodology, decompose nodes, scope tools
3. Self-critique — long nodes have preflight? verify checks
                   OUTCOME not OUTPUT? methodology routing?
4. Validate — call validate_workflow tool, fix errors
5. Loop 2-4 until satisfied
6. Write workflow.yaml + plan-rationale.md
```

`plan-rationale.md` is new — Planner's notes on why it picked the
nodes, tools, methodology it did. Steward reads this when explaining
decisions to the user.

### 4.4 Lifecycle

- **Spawned by**: smooth-mode driver, `camflow plan` CLI, or engine
  during replan.
- **Lives**: 30 s – 2 min typical, until yaml is written.
- **Dies**: explicit `camc stop` + `camc rm` after writing yaml.
- **Distinct from worker agents**: runs **before** the engine main
  loop (no engine.lock yet) in smooth mode; runs **alongside** a
  paused engine during replan.
- Tracked in `.camflow/agents.json` like any other agent (§12).

### 4.5 Modes

- **Non-interactive (default)**. Agent works alone. If input is too
  thin, emits a yaml with explicit `clarify-with-user` nodes that
  surface the question at runtime — does NOT block on stdin.
- **Interactive (`-i`)**. Agent can pause and ask back via stdin.

### 4.6 Cost trade-off (acknowledged)

| Dimension      | Old (single LLM call) | New (agent)       |
|----------------|------------------------|-------------------|
| Latency        | 2-5 s                  | 30 s - 2 min      |
| $ per plan     | cents                  | low dollars       |
| Test fixture   | mock `llm_call`        | mock camc + tools |

Buys: robustness to thin input, self-validation, real replan,
collapses most of camflow-manager.

### 4.7 What this unlocks

- Manager skill collapses from 8 to 3 phases (§10).
- Scouts demote from prerequisite to optional optimization.
- Replan becomes meaningful — Planner can read trace + state and
  reason about what went wrong before drafting the new yaml.
- `plan-quality validator` switches role from post-hoc warning emitter
  to a tool the Planner calls in its own loop.

---

## 5. Layering after this change

Extends [`strategy.md` §1](strategy.md#1-agent-management-strategy):

```
            ┌────────┐  natural language
   USER ◄──►│Steward │◄───── camc send <steward-id> ...
            │ (LLM)  │
            └───┬────┘  events (camc send)        ctl proposals
                │                ▲                    │
                │ structured     │ wakes Steward      │ writes
                ▼ events         │                    ▼ control queue
            ┌─────────────────────────────────────────────┐
            │ Engine  (Python, deterministic — unchanged) │
            │   • lock + heartbeat + state.json           │
            │   • node scheduler + retry/escalation       │
            │   • event emitter (NEW)                     │
            │   • control queue reader (NEW)              │
            │   • Planner spawn on replan (NEW)           │
            │   • agents.json registry writer (NEW)       │
            └────────┬────────────────────────────────────┘
                     │ camc run
                     ▼
              ┌─────────┐  ┌─────────┐  ┌─────────┐
              │worker A │  │worker B │  │worker C │
              └─────────┘  └─────────┘  └─────────┘

       (one-shot, before/alongside engine)
            ┌─────────┐
            │ Planner │   spawned by smooth-mode driver,
            │ (LLM)   │   `camflow plan` CLI, or engine
            └─────────┘   (replan). Dies after writing yaml.

                       Watchdog supervises engine — unchanged.
```

Watchdog still watches the engine. It does **not** watch Steward
(advisory) or the Planner (one-shot).

---

## 6. Idea 1 — Default smooth mode

### 6.1 CLI surface

| Command                          | Mode      | After |
|----------------------------------|-----------|-------|
| `camflow "<request>"`            | smooth    | NEW: planner agent → confirm → run |
| `camflow run <yaml>`             | explicit  | unchanged + steward attaches |
| `camflow run --no-steward <yaml>`| explicit  | NEW: opt out of steward |
| `camflow plan "<request>"`       | plan-only | NEW: now spawns planner agent |
| `camflow plan -i "<request>"`    | plan-only | NEW: interactive planner |
| `camflow resume <yaml>`          | resume    | unchanged + reattaches steward |
| `camflow chat [text]`            | query     | NEW: route to project's steward |
| `camflow ctl <verb> [args]`      | tools     | NEW: steward's verb set (also user-callable) |
| `camflow steward kill`           | mgmt      | NEW: explicit steward termination |
| `camflow steward restart`        | mgmt      | NEW: kill + respawn with summary |
| `camflow steward status`         | mgmt      | NEW: lifecycle state + last activity |

`camflow run`, `camflow resume`, `camflow stop`, `camflow status`,
`camflow watchdog` keep their current semantics — see
[`self-monitoring.md`](self-monitoring.md).

### 6.2 The smooth-mode flow

```
camflow "build watchdog feature with tests, deploy to PDX-098"
   │
   ├─ 1. PROJECT DETECT  (cwd; --project-dir overrides)
   │
   ├─ 2. STEWARD CHECK
   │     does .camflow/steward.json exist + is the steward alive?
   │     → yes: route the request to existing Steward (it will spawn
   │            Planner). Smooth mode exits here.
   │     → no: proceed below
   │
   ├─ 3. PLAN  (spawn Planner agent — see §4)
   │     "Planning… (typically 1-2 min)"
   │     output → .camflow/workflow.yaml
   │              .camflow/plan-rationale.md
   │              .camflow/CLAUDE.md  (if generated)
   │
   ├─ 4. SUMMARIZE
   │     ASCII graph + Planner's rationale headline + warnings
   │
   ├─ 5. COUNTDOWN (interactive; suppressed under --yes)
   │     "Run in 5 s — Ctrl-C abort, e<enter> edit yaml,
   │     r<enter> replan with extra context"
   │
   ├─ 6. KICKOFF (camflow run path, --daemon)
   │     spawns engine + watchdog
   │     spawns Steward (this is the project's first flow)
   │     exits driver
   │
   └─ 7. PRINT chat hint
         "Steward: steward-7c2a — `camflow chat` to ask anything."
```

Step 2 is what changes when a project already has a Steward: subsequent
`camflow "<task>"` invocations route through it instead of spawning a
new Steward. This makes Steward truly the front desk.

### 6.3 Plan output location

| File                          | Owner          | Purpose                               |
|-------------------------------|----------------|---------------------------------------|
| `.camflow/workflow.yaml`      | Planner agent  | the generated plan                    |
| `.camflow/plan-rationale.md`  | Planner agent  | why these nodes/tools/methodology     |
| `.camflow/CLAUDE.md`          | Planner agent  | per-project agent brief               |
| `.camflow/plan-request.txt`   | smooth driver  | original NL request                   |
| `.camflow/plan-warnings.txt`  | Planner agent  | non-fatal warnings                    |
| `.camflow/planner.log`        | Planner agent  | full session log                      |

`camflow run <yaml>` is unaffected — user-named yaml stays put.

### 6.4 Replan path

`camflow ctl replan --reason "..."` (Steward calls, possibly via
user chat):

1. Engine drains current node (or kills it if `--hard`).
2. Engine spawns a new Planner agent with: original
   `plan-request.txt` + replan reason + `state.json` snapshot +
   recent `trace.log` tail + the existing yaml.
3. Planner writes `.camflow/workflow.next.yaml`.
4. State.json gets a `replan_handoff` marker (lessons + completed-
   nodes carry over).
5. Engine re-launches with the new yaml.

This protects the engine's "plan never modified mid-run" invariant
([`strategy.md` §5](strategy.md#5-plan--execute-boundary)).

---

## 7. Idea 2 — Steward agent

### 7.1 Role

**Steward IS:**
- The project's persistent memory across flows and worker generations.
- The user's natural-language interface to everything camflow does in
  this project.
- The decider for "what does this event mean?" / "what do we do now?"
  when the deterministic plan doesn't fit.
- A camc agent named `steward-<project-shortid>`.

**Steward IS NOT:**
- The dispatcher. Engine schedules workers per yaml.
- A writer of `state.json` or holder of `engine.lock`.
- Allowed to call risky `ctl` verbs without user confirmation
  (configurable; see §7.6).
- Reading raw worker logs by default; reads structured events.
- Required. `--no-steward` ships from day one.
- Generating yaml. To change the plan, Steward calls `ctl replan`,
  which spawns a Planner agent.

### 7.2 Lifecycle (project-scoped, never auto-exits)

```
   first camflow run                 explicit human action
   in this project                   `camflow steward kill` or
        │                             `camc rm <steward-id>`
        ▼                                   │
   ┌─────────┐                              │
   │  BORN   │                              │
   └────┬────┘                              │
        │                                   │
        ▼                                   │
   ┌─────────┐  flow ends    ┌─────────┐   │
   │ ALIVE   │◄─────────────►│ ALIVE   │   │
   │(working)│  next flow    │ (idle)  │   │
   └────┬────┘  starts       └────┬────┘   │
        │                         │         │
        │ compaction               │         ▼
        ▼ detected                 │    ┌────────┐
   ┌──────────┐                    │    │  DEAD  │
   │ HANDOFF  │ archive old +      │    └────────┘
   │ DETECTED │ spawn fresh ───┐   │         ▲
   └──────────┘                │   │         │
                               └───┴─────────┘
                                  (forced kill anytime)
```

- **Born** with the first flow in a project. Subsequent flows reattach.
- **Idle** between events: Claude Code agent at the prompt = zero CPU,
  zero token cost.
- **Working** when handling an event or user message: one Claude turn,
  back to idle.
- **Handoff** on detected compaction: fresh Steward spawned with
  `steward-summary.md` + `steward-archive.md` as boot pack; old
  session archived; new id; `agents.json` records both.
- **Reattach** across engine crash + watchdog-driven resume: Steward
  outlives engine restarts.
- **Death** ONLY by:
  - User runs `camflow steward kill` (graceful: 10 s for final summary,
    then `camc stop` + `camc rm`).
  - User runs `camc rm <steward-id>` directly.
  - User runs `camflow steward restart` (kill then respawn).

When a flow ends, Steward stays alive and idle. The user can ask
"how did the last flow go?" days later. The project's REPORT phase
(formerly Manager step 8) collapses into chat.

### 7.3 When Steward wakes (event-driven)

| Trigger                  | Source / kind                  | Wakes? |
|--------------------------|--------------------------------|--------|
| `flow_started`           | engine, after spawn            | yes    |
| `flow_terminal`          | engine, before flow ends       | yes    |
| `flow_idle`              | engine, after flow ends, before steward goes back to idle | yes (writes summary) |
| `node_started`           | engine                         | yes    |
| `node_done` (success)    | engine                         | yes    |
| `node_failed`            | engine                         | yes    |
| `node_retry`             | engine, rate-limited (OQ-3)    | sometimes |
| `escalation_level_change`| engine                         | yes    |
| `verify_failed`          | engine                         | yes    |
| `heartbeat_stale_worker` | engine                         | yes    |
| `replan_done`            | engine, after Planner re-spawn | yes    |
| `engine_resumed`         | engine, after watchdog restart | yes    |
| `checkpoint_now`         | engine, every 20 events / 30 min | yes (writes summary) |
| user message             | `camflow chat` / `camc send`   | yes    |

Steward never polls. Every wake is push-driven via `camc send`.

### 7.4 Event interface (engine → steward)

Events are single JSON objects per `camc send`, prefixed with
`[CAMFLOW EVENT]` so Steward distinguishes them from user messages:

```
[CAMFLOW EVENT] {"type":"node_done","step":7,"node":"compile",
"flow_id":"flow_001","status":"success",
"summary":"compiled 41 files in 12.3s","agent_id":"a1b2c3",
"attempt":1,"escalation":0,"ts":"2026-04-26T10:00:00Z"}
```

Schema (closed set; future additions go through this design doc):

| Field        | All | Description                                   |
|--------------|-----|-----------------------------------------------|
| `type`       | yes | one of the table in §7.3                      |
| `flow_id`    | yes | which flow (Steward sees multiple over time)  |
| `step`       | most| engine step counter (matches trace.log)       |
| `node`       | most| node id this event is about                   |
| `status`     | done/failed | `success` / `fail`                     |
| `summary`    | done/failed | 1-line agent summary (240 chars)       |
| `error`      | failed | `{code, retryable, reason}`                |
| `agent_id`   | started/done | camc id of the worker                 |
| `attempt`    | retry  | 1-indexed attempt                          |
| `escalation` | escalation | 0..4                                   |
| `since_s`    | heartbeat_stale | seconds                            |
| `final`      | flow_terminal | `{status, pc, completed, failed_node}` |
| `new_yaml`   | replan_done | path to new workflow.yaml                |
| `ts`         | yes | ISO-8601 UTC                                  |

**Why skinny.** Steward has filesystem Read but not engine-process
Read. Detail = `camflow ctl read-state` / `read-trace --tail N`.

**Transport.** Existing `camc send`. No new IPC.

**Backpressure.** Engine's per-tick batcher coalesces same-type events
(`node_retry x4 since 30s`).

### 7.5 Tool surface — `camflow ctl`

A new `camflow.cli_entry.ctl` module. Read verbs are local file reads;
mutating verbs queue JSONL to either `.camflow/control.jsonl`
(autonomous) or `.camflow/control-pending.jsonl` (needs confirm).

| Verb                     | Default autonomy | Effect                       |
|--------------------------|------------------|------------------------------|
| `read-state`             | **autonomous**   | print state.json             |
| `read-trace [--tail N]`  | **autonomous**   | print last N trace entries   |
| `read-events [--tail N]` | **autonomous**   | print last N steward events  |
| `read-rationale`         | **autonomous**   | print plan-rationale.md      |
| `read-registry`          | **autonomous**   | print agents.json            |
| `summarize "<text>"`     | **autonomous**   | write to steward-summary.md  |
| `archive-summary`        | **autonomous**   | fold summary into archive.md |
| `ask-user "<q>"`         | **autonomous**   | enqueue to steward-asks.jsonl |
| `pause`                  | **autonomous**   | engine: status → waiting     |
| `resume`                 | **autonomous**   | engine: waiting → running    |
| `kill-worker`            | **autonomous**   | SIGTERM current worker       |
| `spawn --node <id>` `[--brief "<text>"]` | **confirm**      | force a node to be next pc   |
| `skip --reason "<text>"` | **confirm**      | mark current node done       |
| `replan --reason "<text>" [--hard|--graceful]` | **confirm** | re-spawn Planner (§6.4) |

### 7.6 Autonomy configuration

`.camflow/steward-config.yaml` (project-scoped, optional):

```yaml
autonomy: default       # cautious | default | bold

# Optional per-verb override, takes precedence over preset
overrides:
  kill-worker: confirm    # this project: I want to confirm even kills
  spawn: autonomous       # this project: I trust spawn

# Confirm-flow behavior
confirm:
  timeout_minutes: 30     # OQ-11 default: deny on timeout
  channel: chat           # 'chat' (camflow chat reply) | 'inbox-only'
```

| Preset     | Behavior                                                  |
|------------|-----------------------------------------------------------|
| `cautious` | All mutating verbs require confirm. Steward only autonomous on read-* / summarize / ask-user / pause / resume. |
| `default`  | The table in §7.5 — kill-worker autonomous (with cooldown), spawn/skip/replan need confirm. |
| `bold`     | All verbs autonomous; user reviews trace.log post-hoc.   |

**Confirm flow.**

```
1. Steward calls   camflow ctl spawn --node debug
2. ctl writes to   .camflow/control-pending.jsonl
3. Pending entry includes:
     {ts, verb, args, steward_session_id, expires_at}
4. User runs `camflow chat` → sees:
     "Steward wants to spawn node 'debug' (reason: ...). Approve? [y/N/never]"
5. User responds:
     y      → entry moved to .camflow/control.jsonl, engine drains
     N      → entry written to control-rejected.jsonl, Steward notified
     never  → autonomy override updated (verb → confirm? no, → block)
6. Timeout (default 30 min) without response → reject (OQ-11 = B).
   Steward gets a `confirm_timeout` event.
```

**Cooldown for autonomous kill-worker.** A killed worker won't be
killed again within 30 s, to avoid Steward looping. The cooldown
applies per (flow_id, node_id, agent_id) tuple.

### 7.7 Steward's prompt (sketch)

`.camflow/steward-prompt.txt`, written at first spawn, never
regenerated (memory grows in summary/archive instead):

```
You are the Steward agent for camflow project <shortid>.

PROJECT
─────
<absolute path>

YOUR JOB
────────
- You are this project's persistent assistant. You don't go away
  when a flow ends; you stay until a human kills you.
- You receive [CAMFLOW EVENT] messages from engines (one engine per
  active flow). Each event is a small JSON.
- You receive natural-language messages from the user via
  `camflow chat`. Treat unprefixed messages as user input.
- You are NOT a dispatcher. The engine decides which node runs
  next, per workflow.yaml. You can propose corrective actions via
  the `camflow ctl` CLI, subject to autonomy config.

YOUR TOOLS (camflow ctl ...)
──────────
[full table from §7.5]

AUTONOMY
────────
- Some verbs (kill-worker, pause, resume, summarize, ask-user) you
  may invoke autonomously.
- Risky verbs (spawn, skip, replan) need user confirmation. When
  you call them, ctl will queue them; the user gets a prompt next
  time they `camflow chat`.

MEMORY
──────
- .camflow/steward-summary.md holds your current working memory.
- .camflow/steward-archive.md holds older condensed memories,
  one section per past flow.
- On `checkpoint_now` events: call `ctl summarize` with full state.
- On `flow_idle` events: call `ctl archive-summary` to fold into
  archive.

DEFAULT BEHAVIOR
────────────────
- node_done success: stay quiet unless asked.
- node_failed: state cause + engine's retry decision +
  recommendation (or "engine handling, no action").
- heartbeat_stale_worker: probe; if hung, kill-worker.
- user "现在状况？": one paragraph; read state if needed; don't
  dump trace lines.
- user asks about a past flow: read archive first; only call
  read-trace if the answer isn't there.
```

### 7.8 Compaction handoff

Steward sessions WILL hit Claude's context wall — especially in the
new project-scoped, multi-flow lifetime. Three layers of mitigation:

1. **Skinny events** (§7.4) — typical event <300 tokens.
2. **Periodic memory checkpoint.** Every 20 events OR every 30 min,
   engine sends `checkpoint_now`. Steward calls `ctl summarize` →
   `.camflow/steward-summary.md`.
3. **Per-flow archive fold.** On `flow_idle`, Steward calls
   `ctl archive-summary` → folds the current summary's flow-specific
   sections into `.camflow/steward-archive.md` (one section per flow,
   condensed). The summary file resets to a clean slate for the next
   flow's working memory.
4. **Handoff on detected failure.** Response time > 90 s twice in a
   row OR camc reports session compacted → engine spawns fresh
   Steward with `steward-summary.md` + `steward-archive.md` as boot
   pack. Old session archived to `.camflow/archive/`.

### 7.9 Cost

| Phase                    | Cost                                 |
|--------------------------|--------------------------------------|
| Idle                     | zero                                 |
| Per event                | one Claude turn (input bounded by summary + recent events) |
| Long lifetime            | linear in events thanks to checkpoint + archive fold |

`--no-steward` removes Steward entirely → today's exact cost.

---

## 8. Idea 3 — Steward as queryable chat interface

### 8.1 Discovery and `camc list`

Steward is a normal camc agent named `steward-<project-shortid>`.
Visible in `camc list`. Workers (`camflow-<node>`) coexist below.

`camflow status` (existing CLI; see [self-monitoring.md](self-monitoring.md))
gains a Steward row that reflects "alive between flows":

```
$ camflow status
Workflow: /path/to/workflow.yaml   (or "no active flow")
Engine:   ALIVE (pid 12345, heartbeat 5s ago)
Watchdog: ALIVE (pid 12346)
Steward:  ALIVE (steward-7c2a, idle 12m, last flow done 2h ago)   ← NEW
Node:     compile (iteration 3, attempt 1)
Agent:    a1b2c3 (running, 2m 30s)
Pending:  1 confirmation request — `camflow chat` to review        ← NEW
Chat:     camflow chat
```

### 8.2 `camflow chat`

```
camflow chat                       # interactive REPL
camflow chat "现在状况?"           # one-shot send + reply
camflow chat --history             # recent (user, steward) turns
camflow chat --inbox               # unread `ask-user` questions
camflow chat --pending             # pending confirms; reply y/N/never
camflow chat --project <path>      # target a specific project
```

Resolution order for "current Steward":

1. `--project` flag → `<project>/.camflow/steward.json`.
2. cwd has `.camflow/steward.json` → use it.
3. Multiple projects under `~/.cam/projects/` and none in cwd →
   list and ask the user to pick.
4. None found → exit with hint.

**Distinguishing user from engine.** Engine prefixes
`[CAMFLOW EVENT]`. User messages from `camflow chat` arrive without
prefix. Steward's prompt instructs it to treat unprefixed messages
as user input.

### 8.3 Worked examples

```
$ camflow chat "现在状况?"
[Steward] 3/4 nodes done. compile and lint passed; test is on
attempt 2 of 3. ETA ~12 min. Nothing for you to do yet.

$ camflow chat "为啥节点test挂了?"
[Steward] Worker hit AssertionError in test_lock_contention: two
engines acquired the lock simultaneously. From trace, this is a
real bug, not flakes. Recommend a barrier in the test fixture
rather than retrying. Want me to pause? Note: pause is autonomous
in this project.

$ camflow chat "yes pause"
[Steward] Paused.

$ camflow chat "在 PDX-098 上加一台 build 节点"
[Steward] That changes the plan — I'd like to call replan with
that constraint. Replan is a `confirm` verb here, so I've queued
the request. Reply `y` to approve, `N` to cancel.

$ camflow chat --pending
1 pending confirmation:
  ts:    2026-04-26T11:34:12Z (28 min remaining)
  verb:  replan
  args:  reason="add PDX-098 build node before deploy"; mode=graceful
  reply: [y/N/never]
> y
[Steward] Replan approved. Engine will drain the current node,
then spawn a Planner agent with your new requirement.
```

### 8.4 The compaction-recovery benefit

Worker compactions (we hit this 3× today on c906-fix) are now
transparent:

```
[CAMFLOW EVENT] {"type":"node_failed","node":"fix","error":{
"code":"AGENT_CRASH","reason":"context compacted mid-edit"},...}

[Steward, internal reasoning]
Fix worker died to compaction. I have the previous `completed`
actions in state, plus my decision history. I know what bug it
was chasing.

[Steward → ctl, autonomous: spawn is `confirm` by default]
camflow ctl spawn --node fix --brief "Resume the audit-log fix
in src/audit.py around line 213. Previous worker had identified
the off-by-one and was about to write the patch. Don't
re-investigate; finish."

→ queued to control-pending.jsonl
→ user runs `camflow chat --pending`, sees the brief, says y
→ engine spawns fresh worker with brief in active_state.
```

If `spawn` is moved to `autonomous` in this project's config, the
recovery is fully hands-free.

---

## 9. Multi-flow / multi-project scenario

**Scope of one Steward = scope of one `.camflow/` directory = one
project.**

| Setup | Stewards |
|-------|----------|
| 1 project, 1 active flow      | 1 Steward |
| 1 project, sequential flows   | 1 Steward witnesses all |
| 1 project, simultaneous flows | engine.lock prevents this — same as today |
| 3 projects, 3 simultaneous flows | 3 independent Stewards |

Match Huailu's lean. Cross-project coordination is out of scope.

`camflow chat --all "status"` (Phase D, optional) fans the same
question to every Steward and concatenates replies.

---

## 10. Manager skill compression

The 8-phase camflow-manager skill becomes **3 phases**:

| New phase   | Was            | Does                                   |
|-------------|----------------|----------------------------------------|
| **ORIENT**  | parts of GATHER + SETUP | mode pick (CAM vs CLI), domain orient, resource pre-check |
| **KICKOFF** | KICKOFF        | `camflow "<NL>"` (or `camflow run <yaml>`) → exit |
| **REPORT**  | POST           | (mostly absorbed) only when Steward is dead or `--no-steward` was used; reads state.json + trace.log + archive |

Folded into Planner agent: COLLECT, PLAN, REVIEW, CONFIRM, most of
SETUP.

Folded into Steward: most of POST. After flows end, the user just
asks Steward "how did it go?" — no fresh skill invocation needed
unless Steward is gone.

ORIENT is the high-touch path for cold-starting a project. Smooth
mode is the one-liner path. They both end up calling Planner agent
+ engine the same way.

---

## 11. Watchdog interaction (do not break)

[`self-monitoring.md`](self-monitoring.md) ships and must keep
working unchanged.

| Component       | Watches      | Watched by    | Auto-restarts? |
|-----------------|--------------|---------------|----------------|
| Engine          | workers      | Watchdog      | yes (existing) |
| Watchdog        | Engine       | —             | manual         |
| Steward         | engine events| Engine (§7.8) | yes (handoff)  |
| Planner agent   | n/a          | spawning caller | no (one-shot) |

- Watchdog still spawns `camflow resume --daemon` on dead engine;
  doesn't know about Steward or Planner.
- Engine resume reads `.camflow/steward.json`: alive → reattach
  (`engine_resumed` event); dead → respawn with summary+archive
  boot pack.
- **`camflow stop` ordering: watchdog → engine.** Steward NOT
  stopped — it's project-level, not flow-level. Engine clean
  shutdown emits `flow_terminal`; Steward writes its summary;
  then idles.
- **`camflow steward kill` is the only way to stop Steward** (or
  raw `camc rm`). This separation is deliberate: stopping a flow
  shouldn't lose project memory.
- `--no-steward` skips Steward entirely. With both `--no-steward`
  and `--no-watchdog`, daemon behaves exactly like today.

---

## 12. Project-scoped agent registry — `.camflow/agents.json`

Comprehensive record of every camflow agent ever spawned in this
project — alive, completed, killed, archived. Append-only by id;
status fields flip in place.

### 12.1 Schema

```json
{
  "version": 1,
  "project_dir": "/home/hren/work/c906-fix",
  "current_steward_id": "steward-7c2a",
  "agents": [
    {
      "id": "steward-7c2a",
      "role": "steward",
      "spawned_at": "2026-04-26T10:00:00Z",
      "spawned_by": "camflow run (smooth)",
      "status": "alive",
      "tmux_session": "cam-7c2a3f",
      "boot_pack": ".camflow/steward-prompt.txt",
      "session_log": ".camflow/steward-history.log",
      "memory_files": [".camflow/steward-summary.md",
                       ".camflow/steward-archive.md"],
      "flows_witnessed": ["flow_001"]
    },
    {
      "id": "planner-9f3b",
      "role": "planner",
      "spawned_at": "2026-04-26T10:00:05Z",
      "spawned_by": "camflow run (smooth)",
      "flow_id": "flow_001",
      "status": "completed",
      "completed_at": "2026-04-26T10:01:42Z",
      "outputs": [".camflow/workflow.yaml",
                  ".camflow/plan-rationale.md"],
      "session_log": ".camflow/planner-9f3b.log"
    },
    {
      "id": "camflow-build-a1b2c3",
      "role": "worker",
      "spawned_at": "2026-04-26T10:02:00Z",
      "spawned_by": "engine (flow_001 step 1)",
      "flow_id": "flow_001",
      "node_id": "build",
      "status": "completed",
      "completed_at": "2026-04-26T10:04:30Z",
      "result": ".camflow/node-result-history/flow_001-build-a1b2c3.json",
      "session_log": ".camflow/worker-a1b2c3.log",
      "compacted": false
    },
    {
      "id": "camflow-fix-d4e5f6",
      "role": "worker",
      "flow_id": "flow_001",
      "node_id": "fix",
      "status": "killed",
      "killed_at": "2026-04-26T10:08:11Z",
      "killed_by": "steward-7c2a (ctl kill-worker)",
      "killed_reason": "stuck on compaction",
      "result": ".camflow/node-result-history/flow_001-fix-d4e5f6.json",
      "session_log": ".camflow/worker-d4e5f6.log",
      "resumable": true,
      "resume_brief": ".camflow/worker-d4e5f6-brief.md"
    }
  ]
}
```

### 12.2 Roles

| Role | Lifecycle | Tracked |
|------|-----------|---------|
| `steward` | Project-scoped, persistent (§7.2) | one current + all archived predecessors |
| `planner` | One-shot per yaml generation | every spawn |
| `worker` | One-shot per node attempt | every spawn |

### 12.3 Status values

`alive` / `completed` / `failed` / `killed` / `handoff_archived`

Transitions are flipped by the **engine** as the sole writer (atomic
temp-rename, same as state.json). Steward and CLI commands read but
do not write.

### 12.4 What this enables

- Audit trail at agent granularity (more human-readable than trace.log).
- "Resume that killed worker" recovery flow (consult `resumable` +
  `resume_brief`).
- Safe `camc list` interpretation — registry tells us which IDs are
  ours and what role they had.
- Cross-flow lineage: `flows_witnessed` connects a Steward to the
  flows it has seen.

### 12.5 Persistence file `.camflow/steward.json`

A small companion file pulled out of `state.json` (since state.json
is per-flow-run and Steward is project-scoped):

```json
{
  "agent_id": "steward-7c2a",
  "spawned_at": "2026-04-26T10:00:00Z",
  "config_path": ".camflow/steward-config.yaml",
  "summary_path": ".camflow/steward-summary.md",
  "archive_path": ".camflow/steward-archive.md"
}
```

Engine reads this on `camflow run` to decide reattach vs spawn-fresh.

---

## 13. Unified event trace — `.camflow/trace.log` upgrade

We extend trace.log instead of opening a new audit log. trace.log is
already project-scoped append-only JSONL with fsync discipline.

### 13.1 Tagged-union schema

Every entry gains a `kind` field as discriminator. Today's per-step
records become `kind: "step"`.

```jsonc
// Existing per-step record (kind added)
{"kind":"step","step":7,"node_id":"build","input_state":{...},
 "node_result":{...},"output_state":{...},"transition":{...},
 "ts_start":"...","ts_end":"...","duration_ms":2341,"agent_id":"a1b2c3"}

// Agent lifecycle
{"kind":"agent_spawned","ts":"...","actor":"engine","flow_id":"flow_001",
 "agent_id":"camflow-build-a1b2c3","role":"worker","node_id":"build",
 "tmux_session":"cam-a1b2c3","prompt_file":".camflow/node-prompt.txt"}

{"kind":"agent_completed","ts":"...","actor":"engine","flow_id":"flow_001",
 "agent_id":"camflow-build-a1b2c3","duration_ms":47000,
 "result_file":"..."}

{"kind":"agent_killed","ts":"...","actor":"steward-7c2a","flow_id":"flow_001",
 "agent_id":"camflow-fix-d4e5f6","reason":"stuck on compaction",
 "via":"camflow ctl kill-worker"}

// File operations (significant only — see 13.4)
{"kind":"file_written","ts":"...","actor":"planner-9f3b","flow_id":"flow_001",
 "path":".camflow/workflow.yaml","size_bytes":2341,"sha256":"..."}

{"kind":"file_archived","ts":"...","actor":"engine","flow_id":null,
 "from":".camflow/steward-history.log",
 "to":".camflow/archive/steward-7c2a-history.log",
 "reason":"steward handoff"}

// Control / coordination
{"kind":"control_command","ts":"...","actor":"steward-7c2a","flow_id":"flow_001",
 "verb":"replan","args":{"reason":"OOM on test","mode":"graceful"},
 "queue":"pending"}    // or "approved" / "rejected" / "executed"

{"kind":"control_resolution","ts":"...","actor":"user","flow_id":"flow_001",
 "verb":"replan","resolution":"approved"}

{"kind":"event_emitted","ts":"...","actor":"engine","flow_id":"flow_001",
 "to":"steward-7c2a","event_type":"node_failed","payload_size":284}

// Engine / lock / watchdog
{"kind":"engine_started","ts":"...","actor":"engine","pid":12345,
 "flow_id":"flow_001","mode":"daemon"}
{"kind":"engine_stopped","ts":"...","actor":"engine","pid":12345,
 "reason":"clean","exit_status":"done"}
{"kind":"lock_acquired","ts":"...","actor":"engine","pid":12345,"file":"engine.lock"}
{"kind":"lock_stolen","ts":"...","actor":"engine","pid":12346,
 "reason":"prior holder dead","prior_pid":12345}
{"kind":"watchdog_action","ts":"...","actor":"watchdog","pid":12346,
 "action":"restart_engine","reason":"heartbeat stale 65s"}

// Steward-specific
{"kind":"compaction_detected","ts":"...","actor":"engine",
 "agent_id":"steward-7c2a","indicators":["response_time>90s"]}
{"kind":"handoff_completed","ts":"...","actor":"engine",
 "from_agent":"steward-7c2a","to_agent":"steward-7c2a-v2",
 "memory_carried":[".camflow/steward-summary.md",
                   ".camflow/steward-archive.md"]}
{"kind":"flow_started","ts":"...","actor":"engine","flow_id":"flow_002",
 "yaml":".camflow/workflow.yaml","steward":"steward-7c2a-v2"}
{"kind":"flow_terminal","ts":"...","actor":"engine","flow_id":"flow_002",
 "final_status":"done","pc":"deploy"}
```

### 13.2 Common fields

| Field    | Required | Notes |
|----------|----------|-------|
| `kind`   | yes      | discriminator |
| `ts`     | yes      | ISO-8601 UTC |
| `actor`  | yes      | `engine` / `watchdog` / `planner-<id>` / `steward-<id>` / `worker-<id>` / `user` |
| `flow_id`| yes      | flow context, or `null` for project-level events |

### 13.3 Snapshot vs timeline

| Question | File |
|----------|------|
| "Who is alive right now?" | `.camflow/agents.json` (snapshot) |
| "What happened in the last hour?" | `.camflow/trace.log` (timeline) |
| "Why was that worker killed?" | trace.log: grep `kind=agent_killed AND agent_id=...` |

The two stay consistent: every `agent_*` trace entry is paired with
a registry status flip in the same engine tick (atomic write of
both files).

### 13.4 What gets logged as `file_written` / `file_removed`

| File                                  | Logged? | Why |
|---------------------------------------|---------|-----|
| `workflow.yaml`, `plan-rationale.md`, `CLAUDE.md` | yes | long-lived produce |
| `node-result.json` (per worker)       | yes     | important produce |
| `steward-summary.md`, `archive.md`    | yes     | memory checkpoints |
| `node-prompt.txt`, `steward-prompt.txt` | yes   | useful for debug |
| Lock files (`engine.lock`, `watchdog.lock`) | yes | safety-relevant |
| Archive operations                    | yes     | provenance |
| `state.json` (per-tick updates)       | **no**  | implied by step entry |
| `heartbeat.json` (per-30s)            | **no**  | noisy |
| `progress.json`                       | **no**  | ephemeral |

### 13.5 Backward compatibility

- Reader: `entry.get("kind", "step")` so old entries (no kind) are
  treated as steps.
- `camflow evolve report` filters to `kind == "step"` by default; new
  flag `--include-events` opts into all kinds.

### 13.6 Size growth

5–8× increase per step (5–10 events surround each step). Estimates:
light projects ~200 MB/year, heavy CI ~10 GB/year. **No rotation in
v1.** Rotation strategy is deferred until a real project hits a
problem; flow-terminal-rotation is the obvious first lever then.

### 13.7 sha256 of file_written

Default policy (OQ-9 = C): include `sha256` for files < 100 KB; skip
for larger files. Cheap + audit-friendly + bounded compute.

---

## 14. Phased delivery

### Phase A — Foundations

- **Trust model in code**: structured `camflow ctl` dispatcher with
  verb whitelist + arg schema validation; `control.jsonl` and
  `control-pending.jsonl` queues.
- **Planner agent upgrade**: agent-based with internal critique loop;
  `--legacy` flag for one cycle (OQ-8).
- **Steward v0**: project-scoped, never auto-exits. 5 event kinds:
  `flow_started`, `flow_terminal`, `node_started`, `node_done`,
  `node_failed`. Read-only ctl verbs. `--no-steward` flag.
- **`.camflow/agents.json` registry**, engine as sole writer.
- **trace.log tagged-union schema** with `kind` field. Agent lifecycle
  events emitted. `evolve report` updated.
- **`.camflow/steward.json`** persistence file.
- `camflow status` shows Steward row.
- `camflow chat` works (one-shot + `--history`).
- `camflow steward kill | restart | status` subcommands.

Acceptance: calculator demo runs. Planner agent's yaml validates.
User can ask "what's going on?" and get a real answer. Watchdog
suite still passes. trace.log has agent_spawned / agent_completed
entries for every camc agent.

### Phase B — Agency

- **Mutating ctl verbs**: `pause`, `resume`, `kill-worker`, `spawn`,
  `skip`. `replan` is Phase C.
- **Autonomy config**: `.camflow/steward-config.yaml`, three
  presets, per-verb override.
- **Confirm flow**: `control-pending.jsonl`, `camflow chat --pending`,
  timeout-deny (OQ-11 = B), `[y/N/never]` semantics.
- Event set extended: `node_retry` (coalesced), `escalation_*`,
  `verify_failed`, `heartbeat_stale_worker`, `engine_resumed`.
- Compaction handoff (checkpoint + archive fold + auto-respawn).
- Engine reattaches Steward across `camflow resume` and watchdog
  restart.

Acceptance: an RTL-style workflow runs across a worker compaction;
Steward respawns the worker with brief, no human intervention; user
gets prompted before any `confirm` verb is applied.

### Phase C — Smooth mode + replan + manager compression

- `camflow "<request>"` smooth-mode entry point.
- `camflow plan -i` interactive Planner.
- `camflow ctl replan` end-to-end with Planner re-spawn, controlled
  engine restart, state carry-over.
- `e<enter>` and `r<enter>` countdown handles.
- camflow-manager skill compressed to ORIENT / KICKOFF / REPORT.

Acceptance: `camflow "build a calculator with tests"` runs end-to-end
without yaml. `camflow chat "上次跑的怎么样"` answers correctly days
after the flow ended (REPORT collapsed into chat).

Phases A and B are decoupled. Phase C depends on B (replan) and A
(agent Planner).

---

## 15. Open questions

Numbered persistently across revisions.

### OQ-1. Steward default — opt-in or opt-out?

A. Opt-out (`--no-steward`). Recommended. ⭐
B. Opt-in (`--with-steward`).

### OQ-2. Plan output location — `.camflow/` vs cwd?

A. `.camflow/workflow.yaml`. Recommended. ⭐
B. cwd.

### OQ-3. Should Steward see every retry event?

A. Every retry, coalesced.
B. Only escalation_level changes.
C. Engine-side rate limit. Recommended. ⭐

### OQ-4. `camflow ctl` authentication?

Recommendation: ship without auth in v0; trust boundary same as
state.json. Document.

### OQ-5. `flow_terminal` user-visible output

A. `.camflow/steward-summary.md` only.
B. engine.log only.
C. Both. Recommended. ⭐

### OQ-6. `replan` hard or graceful default?

A. Always graceful.
B. Always hard.
C. Steward chooses via flag, default graceful. Recommended. ⭐

### OQ-7. Multiple-project chat fan-out?

A. Ship `camflow chat --all` in Phase A.
B. Wait until users ask. Recommended. ⭐

### OQ-8. Planner legacy fallback duration?

A. Drop immediately.
B. Keep `--legacy` for one release cycle. Recommended. ⭐
C. Keep indefinitely.

### OQ-9. `file_written` sha256 policy?

A. Always.
B. Never.
C. Files < 100 KB only. Recommended. ⭐

### OQ-10. Default autonomy preset?

A. `default` (mixed: kill-worker autonomous, spawn/skip/replan
   confirm). Recommended. ⭐
B. `cautious` (all mutating need confirm).

### OQ-11. Confirm timeout behavior?

A. Synchronous (block forever until y/N).
B. Timeout-deny after 30 min. Recommended. ⭐
C. Timeout-approve after 30 min.

---

## 16. Files added / changed

### New source files

- `src/camflow/planner/agent_planner.py` — agent-based Planner.
- `src/camflow/planner/tools/validate_workflow_tool.py` — Planner's tool.
- `src/camflow/planner/tools/write_yaml_tool.py` — Planner's tool.
- `src/camflow/steward/__init__.py`
- `src/camflow/steward/spawn.py` — boot pack + `camc run`.
- `src/camflow/steward/events.py` — event schema + emitter + batcher.
- `src/camflow/steward/handoff.py` — compaction detection + respawn.
- `src/camflow/steward/autonomy.py` — config loader + verb classifier.
- `src/camflow/registry/agents.py` — agents.json reader/writer.
- `src/camflow/cli_entry/ctl.py` — `camflow ctl` dispatcher.
- `src/camflow/cli_entry/chat.py` — `camflow chat` wrapper.
- `src/camflow/cli_entry/steward.py` — `camflow steward {kill,restart,status}`.
- `src/camflow/cli_entry/smooth.py` — `camflow "<NL>"` entry.
- `src/camflow/backend/cam/event_emitter.py` — engine → steward bridge.
- `src/camflow/backend/cam/control_drain.py` — control queue reader.
- `tests/integration/test_planner_agent.py`
- `tests/integration/test_steward_lifecycle.py`
- `tests/integration/test_steward_chat.py`
- `tests/integration/test_steward_autonomy.py`
- `tests/integration/test_smooth_mode.py`
- `tests/integration/test_agent_registry.py`
- `tests/integration/test_trace_kinds.py`

### Changed files

- `src/camflow/planner/__init__.py` — switch default to agent
  Planner; `--legacy` flag.
- `src/camflow/backend/cam/engine.py` — emit events at hook points;
  drain control queue at top of each tick; spawn Planner on
  `replan`; sole writer of `agents.json`.
- `src/camflow/backend/cam/agent_runner.py` — `--brief` arg from
  Steward `spawn`; register agent in `agents.json` on start /
  finish.
- `src/camflow/backend/cam/tracer.py` — emit kind-tagged entries.
- `src/camflow/backend/persistence.py` — `agents.json` atomic writer.
- `src/camflow/cli_entry/main.py` — `--no-steward` flag; do NOT
  stop Steward on `camflow stop`.
- `skills/camflow-manager/SKILL.md` — compressed to 3 phases.
- `docs/strategy.md` — add "Steward agent" + "Trust model" + update
  planner section.
- `docs/architecture.md` — add `steward/`, `registry/`; rewrite
  `planner/`; update tracer schema.
- `docs/self-monitoring.md` — `camflow stop` does not touch Steward.
- `docs/roadmap.md` — Phase A/B/C in timeline.

### New `.camflow/` files

| File                          | Owner      | Notes                              |
|-------------------------------|------------|------------------------------------|
| `agents.json`                 | engine     | project agent registry             |
| `steward.json`                | engine     | current Steward pointer            |
| `steward-config.yaml`         | user / engine | autonomy + confirm config       |
| `steward-prompt.txt`          | engine     | Steward's boot pack                |
| `steward-events.jsonl`        | engine     | mirror of events sent to Steward   |
| `steward-summary.md`          | Steward    | current working memory             |
| `steward-archive.md`          | Steward    | per-flow condensed history         |
| `steward-asks.jsonl`          | Steward    | `ask-user` queue                   |
| `steward-history.log`         | engine     | archived session on handoff/death  |
| `control.jsonl`               | engine     | drained verb queue (autonomous)    |
| `control-pending.jsonl`       | ctl        | awaiting user confirmation         |
| `control-rejected.jsonl`      | ctl        | rejected / timed out               |
| `plan-rationale.md`           | planner    | Planner's design notes             |
| `planner.log`                 | planner    | Planner session log                |
| `plan-warnings.txt`           | planner    | non-fatal Planner warnings         |
| `plan-request.txt`            | smooth driver | original NL request             |
| `archive/`                    | engine     | rotated session logs               |

---

## 17. What this design does NOT change

- **The DSL** ([`strategy.md` §4](strategy.md#4-dsl-reference)).
- **Engine's main loop, lock semantics, heartbeat, watchdog rules**
  ([`self-monitoring.md`](self-monitoring.md)).
- **Engine remains the sole dispatcher** and sole writer of
  `state.json`. This is the load-bearing invariant of the trust
  model (§3).
- **Four node execution paths**
  ([`strategy.md` §1](strategy.md#1-agent-management-strategy)).
- **state.json schema.** New persistence concerns (registry, Steward
  pointer, control queues) live in new files.
- **plan/runtime boundary**
  ([`strategy.md` §5](strategy.md#5-plan--execute-boundary)) —
  Planner runs to completion before yaml is loaded; yaml is never
  modified mid-run; replan = controlled restart with a new yaml.
- **trace.log file location and append-only nature.** The schema
  evolves to tagged-union (§13) but the file is still one
  per-project JSONL the way it is today.
- **The yaml format** the Planner produces. Internal Planner
  upgrade is invisible at the DSL boundary.

---

## 18. Phase 2 preview

After Huailu signs off this spec, Phase 2 produces a
commit-by-commit implementation plan grounded in §16 and ordered
by §14. Rough estimate at full pace:

| Phase | Commits | Days |
|-------|---------|------|
| A     | ~12     | ~6   |
| B     | ~10     | ~5   |
| C     | ~6      | ~3   |

Watchdog regression suite must stay green throughout. Every commit
ships with the test that exercises its new surface.
