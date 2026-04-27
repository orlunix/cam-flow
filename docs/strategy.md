# cam-flow Strategy

**This is the single source of truth for how cam-flow works.** Read
this first. Everything else in `docs/` is more detailed or more
specific:

- `architecture.md` — code-level reference (every file, module, function)
- `evaluation.md` — metrics, how we measure whether cam-flow is working
- `roadmap.md` — shipped / in-flight / planned work
- `ideas-backlog.md` — every idea ever discussed, with status
- `lessons-2026-04-19.md` — production-run lessons (historical)
- `cam-phase-plan.md` — original CAM-phase design document (historical)

If any document in `docs/` contradicts this one, **this one wins** —
flag the conflict and open a doc PR.

---

## 1. Agent Management Strategy

**Every agent node spawns a NEW camc agent. No session agent, no
agent reuse, no foreman.**

```
node start:   camc run --name camflow-<node> --path <project>
              → new agent, fresh context
node running: engine polls .camflow/node-result.json (file signal)
node done:    engine reads result → camc stop → camc rm
              → agent destroyed
```

### Four node execution paths

The DSL's `do:` field decides which path runs:

| Form              | How it runs                                        | LLM? |
|-------------------|----------------------------------------------------|------|
| `shell <cmd>`     | `subprocess.run()` in engine process                 | No   |
| `cmd <cmd>`       | Alias for `shell`. Accepted for back-compat.         | No   |
| `agent <name>`    | `camc run` with agent definition loaded from         | Yes  |
|                   | `~/.claude/agents/<name>.md`; persona + tools + skills |      |
|                   | injected into the prompt.                             |      |
| `skill <name>`    | `camc run` with prompt "invoke skill X and follow    | Yes  |
|                   | its instructions."                                   |      |
| `<free text>`     | Inline prompt. `camc run` with the free text as the  | Yes  |
|                   | task, anonymous default agent.                      |      |

### Engine owns the agent lifecycle — end to end

- **Start**: `camc run` (no `--auto-exit`). Prompt is written to
  `.camflow/node-prompt.txt` because tmux paste corrupts long
  multi-line prompts.
- **Completion detection**: the engine polls for `.camflow/node-result.
  json` appearing on disk. **File-first, status-second** — camc's
  `status` field is consulted only as a secondary hint to detect the
  rare case where the agent process actually died.
- **Stop**: `camc stop` (graceful).
- **Remove**: `camc rm --force` — kills tmux, deletes socket, drops
  the agent from `agents.json`.
- **Crash recovery**: `state.current_agent_id` is persisted before
  spawning; on resume, the engine reconciles the orphan (stop + rm
  it, mark the current node as a retry).

**No agent outlives its node.** This is intentional:

- Every node gets a clean, auditable context — no leakage across steps.
- An agent that hangs or dies doesn't poison the next node.
- The Python engine is the single control plane — it holds the state,
  the trace, the transitions. Agents are workers, not controllers.

There is NO "foreman agent" or "session agent" that lives across
multiple nodes. The engine is a Python process, not an agent.

---

## 2. Context Management Strategy

**Stateless execution.** Every agent starts with a blank context. Any
information an agent needs from a prior node must travel through
`state.json` — there is no other channel.

### Context sources, in prompt order

1. **`CLAUDE.md`** — project-level domain knowledge. Stable across
   the whole workflow; prompt-cacheable by the LLM provider. Edit
   this when the user says "make sure you always know about X."
2. **`--- CONTEXT ---` fence** — the six-section state block,
   rendered from `state.json`:
   - Active task
   - Completed actions (what previous nodes did, capped at 8 most
     recent)
   - Active state (arbitrary kv pairs the plan declared)
   - Blocked (most recent failure details, if any)
   - Test output (latest round full; older rounds archived as
     one-line summaries in `test_history`)
   - Lessons learned (append-only, deduped, capped at 10 FIFO)
3. **Methodology hint** — if the plan specified one
   (`methodology: rca` etc.), the matching paragraph from the
   methodology router.
4. **Escalation hint** — injected only on retries; ramps up
   L0→L1→L2→L3→L4 based on `state.retry_counts[node]` and capped by
   `escalation_max`.
5. **Agent persona** — if `do: agent <name>` AND
   `~/.claude/agents/<name>.md` exists, its body is injected as a
   fenced AGENT PERSONA block.
6. **Role line** — `"You are executing workflow node 'X'."` (or
   `"You are the '<agent>' agent executing workflow node 'X'."`
    when a persona is present).
7. **Tool scope** — if the plan set `allowed_tools: [...]`, a soft
   constraint line listing them. (Soft — camc doesn't currently
   enforce; the agent is asked to honor it.)
8. **Task** — the `with:` field (or the free-text `do:` value for
   inline prompts), with `{{state.x}}` refs resolved.
9. **Output contract** — boilerplate that tells the agent to write
   `.camflow/node-result.json` when done. **This is how the engine
   knows the agent finished.**

### What the context does NOT include

- The previous agent's conversation history.
- The previous agent's tool-call logs.
- Files the previous agent read (unless the previous agent
  summarized them into a state field).

**`state.json` is the only context bridge between nodes.** Plan
accordingly — nodes that need information from upstream must declare
the bridging state keys explicitly, and upstream nodes must populate
them via `state_updates` in `node-result.json`.

---

## 3. Node Execution Strategy

Every node runs three phases in order: **preflight → execute → verify**.
Preflight and verify are both optional; execute is always present.

```
┌─────────────────────────────────────────────────────┐
│ preflight (optional, cheap shell — 60 s hard cap)   │
│   pass → proceed                                    │
│   fail → PREFLIGHT_FAIL; body skipped entirely      │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────┐
│ execute (one of shell / agent / skill / inline)     │
│   agent writes .camflow/node-result.json            │
│   shell's exit code drives status                   │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────┐
│ verify (optional, cheap shell — 30 s hard cap)      │
│   runs only if execute reported success             │
│   non-zero exit → downgrade to VERIFY_FAIL          │
└─────────────────────────────────────────────────────┘
```

**Preflight answers "can I even start?"** — cheap, seconds, proves
prerequisites exist before an expensive body commits.

**Verify answers "did I succeed?"** — cheap, seconds, proves the
agent's claimed success is real. Verify must check the **OUTCOME**,
not the OUTPUT: a file existing does not prove the goal was met; a
binary compiling does not prove the simulation ran.

If preflight fails, the body does not run — the node fails with
`error.code = PREFLIGHT_FAIL`. If verify fails on an otherwise-
successful body, the node fails with `error.code = VERIFY_FAIL`.
Retry logic then kicks in per `max_retries` / escalation ladder.

---

## 4. DSL Reference

### `do:` forms

See the table in §1. In short: `shell <cmd>` / `cmd <cmd>` (alias) /
`agent <name>` / `skill <name>` / free-text inline. Any string that
doesn't start with a keyword above is an inline prompt.

`agent claude` is NOT special-cased. The name `claude` is treated
like any other: the loader looks for `~/.claude/agents/claude.md`
and, if absent, the node runs with the anonymous default agent
(same fall-through as any unknown agent name). **Prefer inline
prompts over `agent claude` for new workflows** — it's the idiomatic
form for "anonymous task."

### Node fields

| Field             | Required | Purpose                                       |
|-------------------|----------|-----------------------------------------------|
| `do`              | yes      | What to execute (see §1)                      |
| `with`            | no       | Task instructions for agent / skill nodes     |
| `next`            | no       | Next node on success (falls back to done)     |
| `transitions`     | no       | Conditional routing (first match wins)        |
| `verify`          | no       | Shell cmd run AFTER execute; checks outcome   |
| `preflight`       | no       | Shell cmd run BEFORE execute; checks preqs    |
| `methodology`     | no       | `rca` / `simplify-first` / `search-first` /   |
|                   |          | `working-backwards` / `systematic-coverage`   |
| `escalation_max`  | no       | 0–4, cap on escalation ladder (default 4)     |
| `max_retries`     | no       | Per-node retry budget (overrides default)     |
| `allowed_tools`   | no       | Tool subset `[Read, Edit, Bash, ...]`         |
| `timeout`         | no       | Per-node timeout in seconds                   |
| `model`           | no       | Per-node model override (advisory for now)    |
| `set`             | no       | Seed state keys at node entry                 |

### Transitions

```yaml
transitions:
  - if: fail              # match fail status
    goto: fix
  - if: success           # match success status
    goto: test
  - if: output.<key>      # match truthy result.output.<key>
    goto: X
  - if: state.<key>       # match truthy state.<key>
    goto: Y
```

First match wins. If no transition fires and `next:` is set, fall
through to `next`. Otherwise the node's status terminates the
workflow (`done` on success, `failed` on fail).

### Template variables

`{{state.xxx}}` is resolved against `state.json` at execution time,
before `with:`, `preflight:`, and `verify:` are rendered. No eval,
no complex templating — simple string substitution.

---

## 5. Plan / Execute Boundary

**Plan** is decided before the engine starts. **Execute** runs the
plan without ever modifying it.

| Decided by plan (workflow.yaml)        | Decided by runtime           |
|----------------------------------------|------------------------------|
| Which nodes, in what order             | When to poll for the result  |
| `do:` for each node (shell/agent/…)    | How long to wait per tick    |
| `verify:` + `preflight:` conditions    | How to render the prompt     |
| `methodology:` per agent node          | Methodology fallback (router)|
| `escalation_max:`, `max_retries:`      | Default retry budget         |
| `allowed_tools:`                       | Orphan handling on resume    |
| State-variable contract                | Atomic state writes          |

### Rules

1. **The engine never modifies the workflow.** If the plan is wrong,
   the node fails, retries are exhausted, the workflow fails — and a
   human replans. The engine does not self-heal by editing YAML.
2. **Plan overrides runtime heuristics.** A node that declares
   `methodology:` uses that label; the keyword router only runs when
   the plan is silent.
3. **Runtime fills gaps.** `max_retries` defaults to 3; `node_timeout`
   defaults to 600 s; allowed_tools defaults to full set — each only
   when the plan doesn't specify.

See `architecture.md` § "Plan vs Runtime boundary" for the code-level
implementation of each gap.

---

## 6. Preflight Check Principle

Before any expensive operation, run a cheap check proving its
prerequisites exist.

This is **universal**, not hardware-specific:

| Expensive op              | Preflight                          |
|---------------------------|------------------------------------|
| 1-hour RTL sim            | 60 s quick-test run, check trace   |
| 30-min regression         | Run one test, check exit           |
| 40-min build              | Build one module, check exit       |
| Production deploy         | Hit staging health-check endpoint  |
| Large download            | HEAD request, check size + 200     |
| Long-running formal proof | Parse the property set, check N>0  |

### Planner heuristic

The planner's prompt enforces: **any node with `timeout > 5 min`
MUST declare a preflight.** Authors who hand-write workflows should
follow the same rule. Missing preflight on long nodes is the v1
CoreMark failure mode we spent hours debugging — preflight would
have caught it in 60 seconds.

Preflight is NOT verify. Verify runs AFTER the body and checks the
outcome; preflight runs BEFORE and checks the prerequisites.

---

## 7. Inline → Agent → Skill Promotion

DSL v2 gives you three ways to specify "AI does this." Choose the
least indirection that meets your reuse needs.

| Form               | Use when                                     | Lives in                       |
|--------------------|----------------------------------------------|--------------------------------|
| Inline prompt      | One-off task, no reuse, < 3 lines            | `workflow.yaml` only           |
| `agent <name>`     | Reused persona; specific tool scope; pre-    | `~/.claude/agents/<name>.md`   |
|                    | loaded skills                                | (or `<project>/agents/<name>.md` |
|                    |                                              | when it lands — not yet)       |
| `skill <name>`     | Reused procedure — multi-step, opinionated   | `~/.claude/skills/<name>/SKILL.md` |

### Promotion triggers (apply in order, first match wins)

1. **Inline → agent definition** if ANY of:
   - Same prompt text appears in 2+ workflows.
   - Prompt encodes a persona, not just a task ("You are an RTL
     debug engineer who…").
   - Prompt restricts tools or pre-loads skills — those belong in
     frontmatter.
   - Prompt exceeds ~10 lines and would drift across copies.
2. **Agent → skill** if ANY of:
   - Behavior is a procedure, not a persona — the agent always does
     the same multi-step dance.
   - Procedure has its own state machine, error handling, or
     sub-routines that benefit from skill-style structure (numbered
     steps, decision tables, escalation paths).
   - It's worth measuring on its own — trace rollup
     (`camflow evolve report`) aggregates per skill / per agent, but
     inline prompts are anonymous and un-measurable.
3. **Stay inline** otherwise. Don't pre-emptively promote — a skill
   file is a real maintenance cost. "We reused it twice already" is
   a reason; "we might reuse this someday" is not.

### Trace-driven promotion

When trace rollup shows a pattern appearing in **5+ workflows**,
`camflow evolve report` flags it as a skill-extraction candidate.
Promotion path:

```
project-local inline → verified by trace → human approves → global library
```

The human-approval step is deliberate. Automated skill mining could
overwrite hand-tuned skills or mis-cluster unrelated prompts. A
report + explicit approval is the right pace.

---

## 8. camflow CLI Reference

This is what ships **today** (2026-04-26 — Phase A). Commands marked
**(planned)** appear in roadmap / ideas-backlog but are not yet in the
dispatcher.

### Run + resume

```bash
camflow <workflow.yaml> [flags]          # run a workflow (default mode)
camflow run <workflow.yaml> [flags]      # explicit synonym
camflow run --no-steward <wf>            # skip the project-scoped Steward
camflow resume <workflow.yaml>           # resume a stopped/failed workflow
camflow resume <wf> --from <node>        # resume from a specific node
camflow resume <wf> --retry              # force-flip non-running → running
camflow resume <wf> --dry-run            # plan the resume without running
```

### Plan (Phase A: Planner is now an agent)

```bash
camflow plan "<natural-language request>"   # default: spawn Planner agent
  --project-dir <path>                      # project root (writes to .camflow/)
  --timeout <seconds>                       # agent budget (default 180)
  --legacy                                  # use the pre-Phase-A single-shot
                                            # LLM Planner (one Claude call)
  --claude-md <path>                        # override CLAUDE.md location
                                            # (legacy mode only; agent reads
                                            # the file itself)
  --skills-dir <path>                       # override skills dir
  --agents-dir <path>                       # override agents dir
  --domain {hardware,software,deployment,research}
                                            # load a domain rule pack
                                            # (legacy mode only)
  --scout-report <file>                     # include a scout's JSON report
                                            # (legacy mode only)
  --output <path>                           # also copy plan to this path

# Internal tools the Planner agent shells out to during its loop.
# Users normally don't invoke these directly.
camflow plan-tool validate <yaml>           # DSL + plan-quality validators
camflow plan-tool write <yaml>              # atomic write yaml from stdin
                                            # (sandboxed to .camflow/)
```

### Steward

```bash
camflow steward status [-p <dir>]           # alive/dead + flows witnessed
camflow steward kill [-p <dir>]             # explicit human kill
camflow steward restart [-p <dir>]          # kill + spawn fresh
                       [--workflow <yaml>]
camflow chat "<msg>" [-p <dir>]             # send to project's Steward
camflow chat --history [--tail N]           # tail steward-events.jsonl
```

### Steward control plane (`camflow ctl`)

Read-only verbs ship in Phase A; mutating verbs land in Phase B
behind an autonomy / confirm flow.

```bash
camflow ctl read-state [-p <dir>]                       # state.json
camflow ctl read-trace [--tail N] [--kind step|...]     # trace.log
camflow ctl read-events [--tail N]                      # steward-events.jsonl
camflow ctl read-rationale                              # plan-rationale.md
camflow ctl read-registry [--json]                      # agents.json
```

### Status

```bash
camflow status [<workflow.yaml>] [-p <dir>]   # pretty-print engine + watchdog
                                              # + Steward + node + progress
```

### Scout (read-only)

```bash
camflow scout --type skill --query "<capability>"   # skill catalog search
camflow scout --type env   --query <tool> --query <tool>
                                                    # which + --version per tool
camflow scout --type env   --query path:<abs>       # test -e <path>
    --max-candidates N    # default 5
    --max-checks     N    # default 12
    --timeout        S    # default 30
    --pretty              # indented JSON (default: compact)
```

### Trace evolution

```bash
camflow evolve report <project-dir>      # aggregate traces, summary
  --json                                 # machine-readable output
```

### Planned (roadmap)

- `camflow status <dir>` — pretty-print state.json + last trace entry
- `camflow stop <dir>` — stop a running engine via `.camflow/engine.pid`
- `camflow log <dir> [-f]` — tail `engine.log` (`-f` for follow)
- `camflow run <dir>` — ergonomic wrapper that auto-detects
  `workflow.yaml` inside `<dir>` (currently you must pass the YAML
  path explicitly; `camflow <dir>/workflow.yaml` works today)
- `camflow engine <workflow.yaml>` — explicit engine subcommand
  (currently the default-positional run mode doubles as "engine")

### CLI wrapper on PATH

A `bin/camflow` Bash wrapper ships in the repo. Symlink it into
`~/bin/` (or equivalent) and the CLI works without `PYTHONPATH=…
python3 -m camflow.cli_entry.main` boilerplate:

```bash
ln -sf <repo>/bin/camflow ~/bin/camflow
```

---

## 9. Steward agent (Phase A)

A **Steward** is one persistent project-scoped LLM agent per
`.camflow/` directory. It is the user's natural-language interface to
everything camflow does in that project.

### What the Steward IS
- The project's persistent memory across flows. It outlives any single
  flow and `engine` restart; only an explicit human action ends it
  (`camflow steward kill` or `camc rm <id>`).
- The user's chat front-end. `camflow chat "现在状况?"` routes to it.
- The interpreter of structured engine events. The engine pushes
  `[CAMFLOW EVENT] {...}` JSON via `camc send`; the Steward decides
  what (if anything) to say to the user.
- A camc agent named `steward-<project-shortid>`. Visible in
  `camc list` like any other.

### What the Steward IS NOT
- The dispatcher. The engine schedules workers per `workflow.yaml`.
- A writer of `state.json` or `agents.json` — the engine is the sole
  writer of both. (See §10.)
- Required. `--no-steward` ships with engine and skips Steward
  entirely.
- A reader of raw worker logs by default. Read happens through
  `camflow ctl read-trace` / `read-state` / `read-events` —
  structured, scoped, observable.

### Lifecycle

```
   first camflow run                   explicit human kill
   in this project                     `camflow steward kill`
        │                                     │
        ▼                                     ▼
   ┌─────────┐                            ┌───────┐
   │  BORN   │                            │ DEAD  │
   └────┬────┘                            └───────┘
        │                                     ▲
        ▼                                     │
   ┌──────────────────────────────────────────┘
   │ ALIVE
   │   between flows: idle, zero CPU / token cost
   │   on event:      one Claude turn, back to idle
   │   on resume:     reattached, sees engine_resumed event
   └────
```

`flow_idle` and the compaction-handoff path land in Phase B.

### Project memory files (under `.camflow/`)

| File                       | Owner   | Purpose                            |
|----------------------------|---------|------------------------------------|
| `steward.json`             | engine  | pointer: id, paths, spawn time     |
| `steward-prompt.txt`       | engine  | boot pack (written once at spawn)  |
| `steward-events.jsonl`     | engine  | append-only mirror of every event  |
| `steward-summary.md`       | Steward | working memory (Phase B checkpoint) |
| `steward-archive.md`       | Steward | per-flow condensed memory (Phase B) |
| `steward-history.log`      | engine  | session log on handoff (Phase B)    |

### `flows_witnessed`

Each Steward record in `agents.json` carries a `flows_witnessed: []`
list — the flow_ids it saw during its lifetime. Engine appends on
every `flow_started` (idempotent on resume).
`camflow steward status` surfaces the count.

---

## 10. Trust Model — LLMs off the dispatch path

The central tension solved by the Phase A redesign: we want a chat
interface and persistent project memory (that's what an LLM is good
at) **without** trusting an LLM with deterministic state writes,
locks, or scheduling.

```
                    Implementation         Failure containment
   ┌─────────────────────────────────────────────────────────────┐
   │ Engine          │  deterministic Python  │ code review,        │
   │ Watchdog        │  deterministic Python  │ unit tests, atomic   │
   │                 │                        │ state writes        │
   ├─────────────────────────────────────────────────────────────┤
   │ Planner agent   │  LLM                   │ DSL + plan-quality  │
   │  (writes yaml)  │                        │ validators (every    │
   │                 │                        │ loop iteration);    │
   │                 │                        │ engine re-validates │
   │                 │                        │ before launch       │
   ├─────────────────────────────────────────────────────────────┤
   │ Steward         │  LLM                   │ ctl verb whitelist; │
   │  (proposes)     │                        │ Phase B: autonomy   │
   │                 │                        │ config + risky      │
   │                 │                        │ verbs need human    │
   │                 │                        │ confirm             │
   └─────────────────────────────────────────────────────────────┘
```

**LLMs never sit on the critical path.** Every LLM output is filtered
through a deterministic validator/dispatcher before it can change
system state:

- Planner produces a yaml → `camflow plan-tool validate` runs DSL
  + plan-quality every iteration; engine re-validates before
  spawning anything.
- Steward proposes a corrective action → `camflow ctl <verb>` checks
  the verb against a whitelist + arg schema. In Phase B the risky
  verbs (`spawn` / `skip` / `replan`) queue to
  `control-pending.jsonl` and need human approval before drain.
- Workers produce `node-result.json` → `result_reader` validates;
  malformed results synthesize a typed fail.

The agent registry (`agents.json`) and the trace log (`trace.log`)
together let the engine reason about "who is alive right now?" and
"what happened?" without ever asking an LLM.

---

## Glossary

- **agent** — an LLM-driven sub-process spawned via `camc run` to
  execute one workflow node. Destroyed after that node.
- **CAM mode** — the primary execution mode: a long-running Python
  engine process runs the workflow, spawning agents per node.
  Opposite of CLI mode.
- **CLI mode** — single-session execution driven by `/loop
  camflow-runner` inside a user's Claude Code session. Used for
  simple workflows where spawning a full engine + sub-agents is
  overkill.
- **camc** — Coding Agent Manager. Spawns + manages agents locally,
  backed by tmux.
- **camflow-manager** — the user-facing skill. Runs the 8-phase
  lifecycle (GATHER → COLLECT → PLAN → REVIEW → SETUP → CONFIRM →
  KICKOFF → POST). Calls the Planner; launches the Engine; exits.
- **Planner** — `camflow plan` CLI. As of Phase A the default is an
  **agent** Planner: a camc-spawned Claude Code session that
  explores, drafts, self-validates, iterates, and writes the final
  yaml. The pre-Phase-A single-shot LLM Planner is preserved for one
  release cycle behind `camflow plan --legacy`.
- **plan-tool** — `camflow plan-tool` — internal subcommands the
  Planner agent shells out to during its self-validate / write loop.
  Two verbs: `validate` (DSL + plan-quality, prints JSON) and
  `write` (atomic write yaml from stdin, sandboxed to `.camflow/`).
- **Engine** — the Python process that runs workflows in CAM mode.
  Owns agent lifecycle, state, trace, and the agent registry. Sole
  writer of `state.json` and `agents.json`.
- **Runner** — the camflow-runner skill; per-tick executor for CLI
  mode.
- **Scout** — `camflow scout`; read-only probes of the skill catalog
  and environment that ground the legacy Planner's decisions. Demoted
  from prerequisite to optional optimization in the agent Planner
  path (the agent does its own exploration).
- **Steward** — the project-scoped persistent LLM agent (one per
  `.camflow/`). Phase A. The user's chat front-end and the project's
  long-term memory. Never auto-exits — only human action ends it.
  See §9.
- **ctl / control plane** — `camflow ctl <verb>` is the narrow
  whitelisted interface through which the Steward (and humans)
  influence a running engine. Phase A ships read-only verbs;
  mutating verbs land in Phase B with autonomy + confirm flow.
- **agent registry** — `.camflow/agents.json`; the snapshot view of
  every camc agent ever spawned in this project (steward, planner,
  worker), with status. Engine is sole writer; everything else
  reads. See `architecture.md`.
- **trust model** — see §10. LLMs (Planner, Steward) are advisory;
  Engine + Watchdog are the only deterministic dispatchers / state
  writers.
