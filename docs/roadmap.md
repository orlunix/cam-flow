# cam-flow Strategic Roadmap

This is the definitive document for where cam-flow is going and why.
It reflects the CLI and CAM phase work already shipped, the critical
gaps identified against real workflow demands (calculator demo, RTL
hardware verification), a deep investigation of Hermes Agent (Nous
Research), and a review of Akshay Pachaar's "The Anatomy of an Agent
Harness" (April 2026).

---

## 1. Design Principles

These are the engineering principles we follow. Every roadmap item below
traces back to at least one of them.

1. **"If you're not the model, you're the harness."**
   cam-flow is harness infrastructure — workflow state, prompt
   shaping, verification, retries, checkpoints. We do not try to
   replace the model; we make the model usable under real constraints.

2. **Context is a scarce resource.**
   Every token in the prompt must earn its place. Observation masking,
   tool scoping, fenced recall, and structured state all exist to
   keep the window tight and the signal-to-noise ratio high.

3. **Verification is a 2–3× multiplier.**
   Boris Cherny's observation from shipping Claude Code: the single
   biggest lever on quality is verification layers. Our `fix → test`
   loop is the first layer. Lint, typecheck, integration tests, and
   schema validation are further layers we should wire in.

4. **Errors compound exponentially.**
   99% per-step × 10 steps = 90.4%. A harness that "usually works"
   at each step catastrophically fails multi-step workflows. Error
   handling, retry with context, and escalation are not optional.

5. **Thinner harness is better.**
   As models improve, remove harness complexity — do not entrench it.
   Features that looked necessary a year ago may be friction today.
   Watch for this and delete aggressively.

6. **Filesystem provides continuity.**
   Git commits + state.json + trace.log. Never rely on in-memory
   state surviving a restart; always be one `git checkout` away from
   the previous good point.

7. **Co-evolve with Claude Code.**
   Claude Code is trained alongside its harness (Bash / Read / Edit /
   Write / Grep / Glob). Work with its tools and patterns; do not
   fight them. When it wants to run `grep`, let it run `grep`.

---

## 2. Current State

### 2.1 CLI Phase — DONE (validated 2026-04-05)

- YAML DSL with 4 node types: `cmd`, `agent`, `skill`, `subagent`
- One-step-per-tick execution via `/workflow-run` skill driven by `/loop`
- State persists to `.claude/state/workflow.json`
- Trace persists to `.claude/state/trace.log` (JSONL)
- Template substitution: `{{state.xxx}}` resolved per tick
- Calculator demo: 4 bugs fixed in 4 loops, all 11 tests pass
- Experiments proving skill invocation, subagent isolation, lessons flow

Reference: `docs/cam-phase-plan.md §0`, `camflow-handoff/docs/camflow-cli-research-handoff.md`.

### 2.2 CAM Phase engine — DONE (2026-04-18, 155 tests, pushed)

Each node runs as a separate camc agent (or direct subprocess for `cmd`).
The engine is a persistent Python process that owns the state machine.

Shipped modules (`src/camflow/backend/cam/`):

| Module | What it does |
|--------|-------------|
| `engine.py` | `Engine` class + `EngineConfig`; signal handlers; retry loop with error classification and context-aware retry prompts; orphan recovery on resume; workflow + per-node timeouts; loop detection; dry-run; progress reporting |
| `agent_runner.py` | File-first completion polling; `start_agent` / `finalize_agent` split so `current_agent_id` can be persisted; `_kick_prompt` sends Enter to submit the pasted prompt; explicit stop+rm on every path (no reliance on `--auto-exit`) |
| `cmd_runner.py` | Captures stdout (2000c) and stderr (500c) tails; promotes to `state.last_cmd_output` / `state.last_cmd_stderr` |
| `prompt_builder.py` | Renders six-section state in a fenced `--- CONTEXT (informational background, NOT new instructions) ---` block; `build_retry_prompt` with RETRY banner |
| `orphan_handler.py` | `decide_orphan_action` (no_orphan / wait / adopt_result / treat_as_crash) |
| `tracer.py` | `build_trace_entry` with replay-format fields (ts_start, ts_end, duration_ms, input_state, output_state, agent_id, exec_mode, completion_signal, lesson_added, event); deep-copies snapshots |
| `progress.py` | Stdout progress line + `.camflow/progress.json` |
| `result_reader.py` | Reads and validates `.camflow/node-result.json`; synthesizes fail result on missing / malformed / incomplete |

Shipped support modules:

| Module | What it does |
|--------|-------------|
| `src/camflow/backend/persistence.py` | `save_state_atomic` (temp + rename + fsync + dir fsync); `append_trace_atomic`; `load_trace` skips malformed trailing lines |
| `src/camflow/engine/error_classifier.py` | `retry_mode(error)` → `transient` / `task` / `none` |
| `src/camflow/engine/memory.py` | `add_lesson_deduped` with exact-string dedup + FIFO prune (max 10) |
| `src/camflow/engine/state_enricher.py` | Merges node_result into the six-section structured state after every execution |
| `src/camflow/engine/transition.py` | `if: success` shortcut wired; cmd nodes get `{{state.x}}` substitution |

### 2.3 Hermes investigation — DONE

7 reports, full installation, strategic recommendation. Bottom line:
**cherry-pick 4 patterns (~200 lines), do not migrate**. See §9.

### 2.4 Harness article review — DONE (2026-04-18)

Pachaar's "Anatomy of an Agent Harness" (April 2026). Distilled 4
high-leverage improvements applicable to cam-flow (see §5). The
article's 7 principles are codified at §1.

### 2.5 Watchdog + self-monitoring — DONE (2026-04-25)

Engine + Watchdog dual-process pattern with heartbeat, lock,
stale auto-heal, max-restart cap. `camflow status` shows engine +
watchdog state. See `docs/self-monitoring.md` for the full spec.

### 2.7 Phase B: Agency — DONE (2026-04-26)

Builds on Phase A. The Steward graduates from "passive observer with
read verbs" to "advisor with bounded mutating power, confirm flow,
and durable memory across compaction." Ships:

- **Per-agent private directories.** `.camflow/steward/`,
  `.camflow/flows/<flow_id>/planner/`,
  `.camflow/flows/<flow_id>/nodes/<node_id>/attempts/<n>/`. Each
  agent's boot pack / scratch / output lives in its own namespace;
  the engine ferries data between siblings via `state.json` and the
  per-node `result.json` winning slot. Sole-writer discipline
  (engine writes state/agents/trace) is now a path fact, not just
  convention. New `camflow.paths` module is the single source of
  truth for layout helpers.
- **5 mutating ctl verbs**: `pause`, `resume`, `kill-worker`
  (autonomous, with 30s cooldown per `(flow, node, agent)` tuple),
  `spawn`, `skip` (confirm). Engine drains
  `.camflow/control.jsonl` at the top of every tick via the new
  `control_drain` module; verbs queue uniformly through
  `queue_approved` / `queue_pending`. `pause` flips engine status
  to `waiting`; the main loop now sleeps + re-drains on `waiting`
  so `resume` wakes it up without re-running camflow.
- **Autonomy config**: `.camflow/steward-config.yaml` with three
  presets (`cautious` / `default` / `bold`) + per-verb override.
  `set_override(verb, "block")` persists a "user said never" choice
  so the dispatcher refuses with exit 1 next time.
  `confirm.timeout_minutes` controls the per-entry expiry.
- **Confirm flow**: `camflow chat --pending` interactive review
  with `[y/N/never]`. Timeout-deny (OQ-11 = B): expired entries
  auto-rejected before the user is prompted. Both rejected and
  approved entries leave a paired `kind=control_resolution` audit
  entry in `trace.log`.
- **Extended events**: `node_retry` (rate-limited per OQ-3 with a
  30s coalesce window per `(flow, node)`), `escalation_level_change`
  (emitted when escalation actually moves), `verify_failed`
  (engine downgrades success→fail and tells Steward), and
  `heartbeat_stale_worker` (wrapper exposed; engine probe in a
  later phase).
- **Compaction handoff**: `checkpoint_now` event fires every 20
  events / 30 minutes; `flow_idle` fires after `flow_terminal` so
  the Steward archives this flow's working memory.
  `summarize` and `archive-summary` ctl verbs let the Steward
  write to `summary.md` and fold it into `archive.md`.
  New `steward.handoff.handoff_steward(project_dir, reason=...)`
  archives the live `steward/` directory under
  `steward/archive/<old-id>-<ts>/`, folds `summary.md` into
  `archive.md`, spawns a fresh Steward with the predecessor's
  memory carried over, and updates `agents.json` (old →
  `handoff_archived` with `successor_id` + `archived_dir`).
- **Resume reattach via handoff**: when the engine starts and
  finds a registered Steward whose camc agent is dead,
  `_ensure_steward` routes through `handoff_steward` instead of
  doing a fresh spawn. Working memory survives engine restarts.

### 2.6 Phase A: Steward + agent Planner + trust model — DONE (2026-04-26)

Design landed in `docs/design-next-phase.md`. Triage of the 9-commit
implementation wave is in `docs/triage-2026-04-26.md`. Ships:

- **Project-scoped Steward agent** (`steward-<shortid>`). Persistent
  across flows, killed only by explicit human action. Receives 5
  Phase-A events: `flow_started`, `flow_terminal`, `node_started`,
  `node_done`, `node_failed`, plus `engine_resumed` on resume.
- **agents.json registry** — engine is sole writer; tracks every
  steward, planner, and worker spawned in the project, with
  `flows_witnessed` correlation on the Steward record.
- **trace.log tagged-union** with `kind` discriminator. Backward-
  compatible reader convention (missing `kind` → `step`). New
  kinds: `agent_spawned/completed/failed/killed`, `event_emitted`,
  `control_command`, `handoff_completed`, etc.
- **`camflow ctl`** read-only verbs (`read-state`, `read-trace`,
  `read-events`, `read-rationale`, `read-registry`). Mutating
  verbs land in Phase B.
- **`camflow chat`** one-shot send + `--history`.
- **`camflow steward {status,kill,restart}`** lifecycle commands.
- **`camflow status`** shows a Steward row.
- **Agent Planner**: `camflow plan` defaults to a camc-spawned
  Planner agent with internal critique loop. `--legacy` keeps the
  pre-Phase-A single-shot Planner for one release cycle.
  `camflow plan-tool validate|write` are the agent's CLI tool
  surface.
- **Trust model formalized** (strategy.md §10). LLMs (Planner,
  Steward) are advisory; Engine + Watchdog are the only
  deterministic dispatchers / state writers.
- **Self-destruct fix**: registry-scoped cleanup
  (`cleanup_workers_of_flow`) replaces the old name-prefix sweep
  that once self-killed a parent `camflow-dev` agent. See
  `docs/triage-2026-04-26.md` §2 for the full root-cause analysis.
- **Test infra**: global `_block_real_camc` autouse fixture
  prevents tests from spawning real Steward / Planner agents.
  Tests that truly want a real `camc` mark themselves with
  `@pytest.mark.allow_real_camc`.

---

## 3. Critical Gaps (MUST fix)

### 3.1 Stateless node execution — SHIPPED

**Status.** Implemented. Supersedes the earlier "agent reuse within
loops" plan.

**Decision.** Three approaches evaluated; chose **stateless execution +
structured state** (serverless-function pattern): predictable,
recoverable, traceable, debuggable, cache-friendly.

**What shipped.**
- `src/camflow/engine/state_enricher.py` — six-section state schema
- `src/camflow/backend/cam/prompt_builder.py` — fenced CONTEXT block
- `src/camflow/backend/cam/engine.py` — calls `enrich_state` per node
- `examples/cam/CLAUDE.md` — per-project agent-facing template

**Mitigations for the one tradeoff.** Agents re-read code files each
iteration, but `active_state.key_files` and per-entry `file`/`lines`
refs in `completed` direct them to targeted reads rather than blind
search.

**Tests.** 3 new integration tests in `test_stateless_loop.py` + 26
new unit tests in `test_state_enricher.py`. Full suite: 155 passing.

### 3.2 Agent lifecycle + submission — SHIPPED (2026-04-18 hotfix)

**Problem.** The first end-to-end calculator-demo run hit two
production blockers:

1. **Prompt never submitted.** `camc run "<prompt>"` pastes the
   prompt into the TUI input box but does not submit it. Every fix
   agent sat at `❯ <prompt>` for the full 300 s timeout, then got
   killed. calculator.py was never modified.

2. **`--auto-exit` unreliable (known bug #10).** Idle detection is
   flaky — agents do the work but never exit voluntarily. The engine
   waited indefinitely on an agent-owned exit signal that never fired.

**Fix (shipped).**
- Added `_kick_prompt(agent_id)` in `agent_runner.py`: polls the
  screen for the TUI prompt char (`❯` / `›` / `>`), sends Enter once
  the prompt is visible. Fallback Enter after 30 s. Idempotent.
- Removed `--auto-exit` from `camc run`. The engine now OWNS the
  agent lifecycle: file-appeared is the primary (and only trusted)
  completion signal. On file present → `camc stop` + `camc rm --kill`.
- Fixed `camc rm --force` → `camc rm --kill` (camc CLI renamed flag).
- Fixed `cli_entry/main.py` (imported the deleted `daemon` module);
  rewrote to use `Engine` + `EngineConfig` with proper CLI flags.

**Validated.** Smoke test (one agent creates `hello.txt`) completes
in ~60 s. Calculator demo ran end-to-end: 12 steps, 4 fix agents, all
11 tests pass.

### 3.3 camc session ID tracking

**Problem.** On reboot/resume, camc searches agents by working
directory path. Multiple agents at `/home/hren` collide. We hit this
bug 3 times in production (agents `l1tcm`, `eab4f56e`, `camflow`).
Wrong session gets adopted; cam-flow's orphan handler cannot save us
if camc itself is confused about identity.

**Fix.** Store `session_id` (the tmux session name, e.g.
`cam-486ffaeb`) in `agents.json`. On resume, adopt by `session_id`
directly, not by path match.

**Scope.** camc fix, not cam-flow — but cam-flow's orphan handling
depends on it. Track as a blocker on the camc team.

**Files touched (camc):** `~/bin/camc` adoption logic,
`~/.cam/agents.json` schema.

### 3.4 Structured state schema — SHIPPED (as part of §3.1)

Implemented in `src/camflow/engine/state_enricher.py`. State fields:
`iteration`, `active_task`, `completed`, `active_state.{key_files,
modified_files}`, `blocked`, `test_output`, `resolved`, `next_steps`,
`lessons`, `failed_approaches`, `escalation_level`, `retry_counts`.

### 3.5 Fenced recall framing — SHIPPED (as part of §3.1)

Implemented in `src/camflow/backend/cam/prompt_builder.py`.

### 3.6 Test program hex generation for RTL workflows

**Problem.** cam-flow v0.1 targets software development. The RV32IMC
hardware verification test revealed a class of workflows we don't
support: generate a test program → compile to hex → run simulation →
analyze waveform.

**Fix.** A category that needs:
- A richer `cmd` type that captures artifact paths (not just stdout)
- An `artifact` reference resolver so `with: run this against
  @artifact://generated.hex` works
- Workflow examples for the RTL domain

**Implementation.**
- `spec/artifact-ref.md` — `@artifact://` reference syntax
- `engine/artifact_ref.py` — resolver
- `examples/rtl-verify/` — reference RV32IMC workflow

Deferred to Month 2.

---

## 4. Exception Handler (high priority)

Inspired by the PUA project (github.com/tanweai/pua, 16k stars) but
with engineering instead of "AI pressure" rhetoric.

### 4.1 Methodology Router

**Problem.** One generic retry prompt for all node categories.

**Fix.** Auto-select a methodology by inspecting the node's `do`
field, then inject the matching procedure.

| Node category | Methodology |
|---------------|-------------|
| debug / fix / repair | **RCA** — reproduce → isolate → hypothesize → verify |
| build / compile / package | **Simplify-first** |
| research / analyze / investigate | **Search-first** |
| architecture / design / refactor | **Working-backwards** |
| test / verify / validate | **Systematic coverage** |

**Files.**
- `src/camflow/engine/methodology_router.py` (new)
- `src/camflow/backend/cam/prompt_builder.py` (add `_methodology_block`)
- `tests/unit/test_methodology_router.py` (new)

### 4.2 Failure Escalation Ladder

**Problem.** Flat retry loop; real debugging sometimes needs a
qualitative shift in strategy.

**Fix.** Per-node level (L0..L4), advances on each consecutive
failure, resets on success or node change.

| Level | Name | Intervention |
|------:|------|------|
| L0 | Normal | Standard retry with context |
| L1 | Rethink | Force a fundamentally different approach |
| L2 | Deep Dive | Require source reading + 3 hypotheses before fix |
| L3 | Diagnostic | Full checklist: files, deps, env, logs, state |
| L4 | Escalate | Save diagnostic bundle; enter `waiting` for human |

**Files.**
- `src/camflow/engine/escalation.py` (new)
- `src/camflow/backend/cam/engine.py` (wire into retry branch)
- `src/camflow/backend/cam/prompt_builder.py` (add `escalation_level` param to `build_retry_prompt`)
- `tests/unit/test_escalation.py` (new)

### 4.3 PreCompact State Preservation — DESCOPED

Stateless execution means agents never run long enough to hit
compaction. State that previously needed preservation already lives
in state.json and is re-injected via the fenced CONTEXT block.

### 4.4 Rationale

- **Right tool for the job** — methodology routing
- **Graduated response** — escalation ladder

Not "AI pressure". Just engineering.

---

## 5. Harness Quality Improvements (from Agent Harness review)

Four high-leverage improvements from Pachaar's article, ordered by
effort-to-impact ratio.

### 5.1 HQ.1 Context Positioning — "Lost in the Middle" fix

**Priority.** Week 1 (tiny change, big impact).
**File.** `src/camflow/backend/cam/prompt_builder.py`.

**Problem.** Stanford's "Lost in the Middle" result: LLMs show 30%+
accuracy drop when critical content falls in mid-window positions.
They attend to the start and end. Our current prompt is:

```
Role header → CONTEXT block → Task body → Output contract
```

The CONTEXT (which carries the agent's working knowledge — completed
actions, test output, lessons, failed approaches) sits in the middle
where the model attends least.

**Fix.** Reorder so CONTEXT leads and the task is the final section
before the output contract:

```
CONTEXT block → Role header → Task body → Output contract
```

This also matches how Claude Code's own prompting treats system
preamble vs. user message: stable context up top, fresh task at the
bottom.

**Validation.** Re-run calculator demo after reorder, compare
iteration count (expect ≤ current 4 fix rounds; may drop to 2–3 if
agents engage with CONTEXT more reliably).

### 5.2 HQ.2 Observation Masking

**Priority.** Week 2.
**File.** `src/camflow/engine/state_enricher.py`.

**Problem.** JetBrains Junie pattern: keep only the CURRENT round's
full tool output; summarize older rounds. Our `state.test_output` is
already overwritten each round (good), but we don't track the history
of prior rounds at all — the agent has no sense of "is this run
making progress" across iterations.

**Fix.** Add a `test_history` field (summary only) alongside the
full-fidelity `test_output`:

```json
{
  "test_output": "FAILED test_factorial ...\n(full latest run, 2000 char cap)",
  "test_history": [
    {"iter": 1, "summary": "6 passed, 5 failed"},
    {"iter": 2, "summary": "7 passed, 4 failed"},
    {"iter": 3, "summary": "9 passed, 2 failed"}
  ]
}
```

Rendered in the CONTEXT block so the agent sees trajectory without
re-reading N full test outputs.

**Implementation.**
- `state_enricher._capture_test_output` also extracts a one-line
  summary (regex on pytest `N passed, M failed` line) and appends to
  `test_history` (bounded at 10 entries, FIFO).
- `prompt_builder._render_test_output` renders both the history
  trajectory and the latest full output.
- Unit test in `tests/unit/test_state_enricher.py` for history growth
  and cap.

### 5.3 HQ.3 Per-Node Tool Scoping

**Priority.** Week 3.
**Files.** `workflow.yaml` schema, `src/camflow/backend/cam/agent_runner.py`.

**Problem.** Vercel removed 80% of their agent's tools and got better
results. We currently pass the full tool set to every agent. `analyze`
doesn't need Write; `fix` doesn't need WebSearch.

**Fix.** Optional `allowed_tools` field in each node:

```yaml
analyze:
  do: agent claude
  allowed_tools: [Read, Glob, Grep, WebSearch]  # read-only
  with: ...

fix:
  do: agent claude
  allowed_tools: [Read, Edit, Write, Bash]      # no search, can edit
  with: ...
```

`agent_runner.start_agent` passes this through to `camc run
--allowed-tools <list>`. Default: unchanged (all tools) for backward
compatibility.

**DSL update.** `engine/dsl.py::validate_node` accepts the new field
(validate that every entry is a known Claude Code tool name).

### 5.4 HQ.4 Multi-Layer Verification

**Priority.** Week 3.
**File.** Workflow example templates (no engine change required).

**Problem.** We verify with pytest. That catches logic bugs but not
syntax errors, type errors, or lint-class issues — a fix agent can
write broken Python that fails with a cryptic traceback, wasting a
full pytest round. Cherny's point: verification improves quality 2–3×
*per layer*, so stacking cheap layers before the expensive one is a
huge win.

**Fix.** Canonical workflow template:

```yaml
fix:
  do: agent claude
  with: Fix one bug.
  next: lint

lint:
  do: cmd python3 -m ruff check calculator.py --fix
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: typecheck

typecheck:
  do: cmd python3 -m mypy calculator.py
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: test

test:
  do: cmd python3 -m pytest test_calculator.py -v
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: done
```

Ship as `examples/cam-verified/` reference workflow, document the
pattern in `docs/patterns/multi-layer-verification.md`.

### 5.5 Plan vs Runtime boundary — SHIPPED (2026-04-18)

**Principle.** The plan (workflow.yaml) is authoritative. Runtime
heuristics (keyword-based methodology routing, default retry budgets,
agent self-reports) are good first-order defaults but must not
override explicit plan directives.

**Shipped.** New optional fields on any node:

- `methodology: "<label>"` — explicit methodology hint; overrides
  the keyword router in `engine/methodology_router.py`.
- `escalation_max: N` — caps the escalation ladder at Ln
  (0..4). Non-critical nodes stay at a polite "rethink" instead of
  promoting to human-escalate.
- `max_retries: N` — per-node override of
  `EngineConfig.max_retries`.
- `verify: "<shell cmd>"` — after an agent reports success, engine
  runs this cmd; non-zero exit downgrades the result to
  `status=fail` with `error.code=VERIFY_FAIL`. Short 30 s timeout
  (verify is a proof, not real work).
- `allowed_tools: [...]` — per-node tool scoping (was §5.3 HQ.3,
  now formally part of the plan boundary).
- `timeout: N` — per-node wall-clock timeout override.

**Files touched.**
- `src/camflow/engine/dsl.py` — `NODE_FIELDS` extended.
- `src/camflow/backend/cam/prompt_builder.py` — plan-first
  methodology + `escalation_max` forwarded to `get_escalation_prompt`.
- `src/camflow/engine/escalation.py` — `max_level` parameter.
- `src/camflow/backend/cam/engine.py` — `_apply_verify_cmd` hook
  after `finalize_agent`; `max_retries` node override.
- `tests/unit/test_plan_priority.py` — 14 new cases. Full suite:
  232 passing (was 218).

---

## 6. Checkpoint System

Anthropic's "Ralph Loop" uses git commits as atomic checkpoints.
cam-flow adopts the same discipline: every successful fix becomes a
commit, every run is recoverable.

### 6.1 CP.1 Git-based Checkpoints

**Priority.** Week 2–3.
**Files.** `src/camflow/engine/checkpoint.py` (new), engine.py
integration.

**Flow.**
- After each successful `agent` node (where `state_updates.files_touched`
  is non-empty), engine auto-commits:
  `git add -A && git commit -m "camflow: <node_id> iter <N> — <summary>"`
- On workflow failure, user runs `git log --oneline` to inspect every
  fix step; `git revert` any bad ones.
- On engine resume, `checkpoint.py` reads git log to understand what
  was already done (belt-and-suspenders alongside state.json).

### 6.2 CP.2 Checkpoint Storage Modes

Three modes, workflow.yaml top-level config:

```yaml
checkpoint:
  mode: local          # default: commit locally, no push
  # mode: branch       # commit to dedicated branch camflow/<workflow-id>
  # mode: remote       # commit + push after each successful node
  auto_commit: true
  branch_prefix: "camflow/"
```

**local (default).** `git init` if needed; commits to current branch;
no remote required.

**branch.** Creates `camflow/<workflow-id>` at start; all commits go
there; original branch untouched. User merges or discards when done.

**remote.** Same as branch + `git push` after each commit. Requires
configured remote. Backup + collaboration.

### 6.3 CP.3 What Gets Committed

Include:
- Source files the agent modified (the fixes themselves)
- `.camflow/state.json` (current workflow state)
- `.camflow/trace.log` (execution history)

Exclude (ephemeral, via `.gitignore` updates):
- `.camflow/node-prompt.txt`
- `.camflow/node-result.json`
- `.camflow/progress.json`

### 6.4 CP.4 Restore

Two new CLI verbs:

```
camflow history             # print checkpoint log with summaries
camflow restore <sha>       # git checkout <sha> + rewrite state.json
```

`camflow history` parses `git log --grep '^camflow:'` and formats as
a table. `camflow restore` checks out the commit and reloads state.

---

## 7. Should-Have (high value)

### 7.1 Skill evolution Phase 1 — trace rollup + measurement

**Problem.** GEPA's pitch is "40% faster self-evolving skills" but
it's an offline $2–10/run tool that doesn't ship with Hermes runtime.
cam-flow has better raw material: every node's pass/fail is recorded
in `trace.log` with typed errors and durations.

**Plan.** New CLI `cam evolve report`:
- Reads all `trace.log` files under a project (or a time range)
- Aggregates per-skill: total runs, pass rate, mean duration, top
  failure categories, retry frequency
- Output: `~/.cam/evolve/report.json` + ASCII dashboard
- No mutation — measurement only. Phases 2+ add mutations.

**Files (new).**
- `src/camflow/evolve/__init__.py`
- `src/camflow/evolve/rollup.py`
- `src/camflow/evolve/report.py`
- `src/camflow/evolve/cli.py`

### 7.2 Port 3 hermes-CCC core skills

| Port | Target | Purpose |
|------|--------|---------|
| `hermes-route` | `~/.claude/skills/cam-route/SKILL.md` | Task triage |
| `systematic-debugging` | `~/.claude/skills/systematic-debugging/SKILL.md` | 10-phase debug procedure |
| `subagent-driven-development` | `~/.claude/skills/subagent-driven-development/SKILL.md` | Decomposition |

Copy, rename `hermes-*` → `cam-*`, adapt examples.

### 7.3 Iteration budget with cmd-refund

**Problem.** `max_node_executions` counts cmd and agent nodes
equally. `cmd` nodes are cheap (no LLM) and shouldn't consume the
same budget.

**Fix.** Two budgets:
- `max_agent_iterations` (default 20) — counts agent / subagent / skill
- `max_node_executions` (default 50) — counts all (loop-detection guard)

### 7.4 Dry-run mode polish

Enhance to:
- Show both happy and `if fail` reachability
- Report unreachable nodes
- Show max agent iterations estimate
- Report unresolved `{{state.*}}` references

### 7.5 Planner — `camflow plan` — SHIPPED (2026-04-19)

**Motivation.** Writing workflow.yaml by hand is slow and drifts from
the current best-practice conventions (methodology / escalation_max /
allowed_tools / max_retries / verify). A one-shot generator takes a
natural-language request and produces a valid plan with the right
conventions baked in.

**Shipped.** `src/camflow/planner/`:
- `generate_workflow(request, claude_md_path, skills_dir, env_info,
  llm_call)` → validated workflow dict.
- Pluggable LLM backend (anthropic SDK → claude CLI fallback).
- Three few-shot examples exercise the complexity range.
- DSL validator + plan-quality validator (errors vs warnings).
- `camflow plan "<request>"` CLI writes workflow.yaml, prints an
  ASCII graph and any warnings.

**Next.** Phase-2 planner improvements (not shipped yet):
- "Replan on failure" — when a workflow fails at L4, feed the
  diagnostic bundle back to the planner and produce a revised plan.
- A/B test planner prompt variants against the trace rollup.
- Swap the claude-CLI backend for direct anthropic SDK with prompt
  caching once ANTHROPIC_API_KEY is wired up.

---

## 8. Nice-to-Have (future)

| # | Feature | Target phase |
|---|---------|-------------|
| 10 | Parallel node execution | Month 3+ |
| 11 | SDK Phase (direct Anthropic API, no camc) | Month 3+ |
| 12 | Skill evolution Phase 3–4 (automated mutation + A/B testing) | Month 3+ |
| 13 | Hermes as a cam adapter — `cam run hermes "task"` | Month 3+ |
| 14 | Webhook event ingress (spec exists, not implemented) | Month 3+ |

---

## 9. Hermes Comparison (team reference)

### 9.1 Marketing vs reality

| Claim | Reality |
|-------|---------|
| "Auto-creates skills every 15 tool calls" | Trigger is 5+ calls; no config flag; quality unreliable; can overwrite manually-tuned skills |
| "Sub-agent delegation" | Tool exists but rarely triggers automatically |
| "GEPA self-evolution, 40% faster" | Separate repo, offline, $2–10/run |
| "Self-improving agent" | Saves workflows as markdown notes |

### 9.2 Where each wins

| Dimension | Hermes | cam-flow |
|-----------|--------|---------|
| Messaging platforms (Slack/Discord) | Strong | Out of scope |
| Persistent memory | Built-in (vector) | Via trace + state |
| Easy setup | One-file install | camc + python |
| Multi-machine fleet | Not designed for it | Native (cam contexts) |
| Auto-confirm for Claude Code dialogs | N/A | First-class |
| DAG / loop / wait / resume workflows | Limited | Core |
| NVIDIA-internal integration | None | Via TeaSpirit + AI CLI |
| Structured workflow DSL | No | YAML DSL |
| Self-evolution | GEPA (offline, paid) | Trace-driven (coming) |

### 9.3 Conclusion

Complementary, not competing. Cherry-pick patterns (§3.4, §3.5, §7.2),
don't migrate.

---

## 10. Timeline

| Phase | Items | Impact |
|-------|-------|--------|
| **✅ Shipped** (Apr 18) | §3.1 stateless execution · §3.2 agent lifecycle fix (kick + no auto-exit) · §3.4 structured state · §3.5 fenced recall · calculator demo passes end-to-end | Working CAM engine; demo proven |
| **✅ Shipped** (Apr 19) | Final skill architecture: camflow-manager (sole user interface, 8-phase lifecycle) + camflow-runner (CLI per-tick executor) + Planner (`camflow plan` CLI) + Engine (Python CAM process). Deprecated cam-flow / camflow / camflow-creator skills. | Clean four-component split; manager is the only thing users talk to |
| **✅ Shipped** (Apr 19, DSL v2) | `shell`/`agent <name>`/`skill`/inline `do` forms; `agent_loader.py` reads `~/.claude/agents/<name>.md` (persona/tools/skills/system prompt); `preflight:` two-layer validation with `PREFLIGHT_FAIL` early-exit; `model:` per-node override; domain rule packs (`hardware`/`software`/`deployment`/`research`) on `camflow plan --domain`; planner prompt embeds OUTCOME-not-OUTPUT and one-node-one-deliverable rules. 292 unit tests passing. | Fail-fast on expensive nodes; reusable agent identities; domain-aware plans |
| **✅ Shipped** (Apr 19, planner scouts) | `camflow scout --type {skill,env}` CLI; `planner/scouts.py` (skill-scout via `skillm search` + fallback walk; env-scout via `which`/`--version`/path probes); `camflow plan --scout-report` flag (cap 3); planner prompt renders scout reports section + describes scouts in PLANNING_RULES; camflow-manager Phase 3.0 SCOUT documents the scout-then-plan pattern. Inline → agent → skill promotion guideline added to architecture.md. 320 tests passing. | Planner picks skills/tools from real catalog probes, not guesses |
| **✅ Shipped** (Apr 19, resume + wrapper) | `camflow resume <wf>` subcommand: auto-flip `failed`/`aborted`/`engine_error` → `running`, `--from <node>` jumps pc + clears blocked/last_failure, `--retry` for opt-in flip from `done`/`waiting`, `--dry-run` mode, retry-budget reset for resumed pc only. `bin/camflow` wrapper script (symlink-friendly via `readlink -f`) installed via PATH symlink. 343 tests passing. | Stopped/failed workflows resume in one command; CLI usable without PYTHONPATH boilerplate |
| **✅ Shipped** (Apr 19, agent-claude cleanup) | Removed the `agent claude` sentinel — the name `claude` is no longer special-cased in `_resolve_agent_def`. Every `agent <name>` form uniformly tries `~/.claude/agents/<name>.md`; absent file → anonymous fall-through. Planner prompt + few-shot examples + 14 test files migrated to inline prompts or `agent placeholder`. 344 tests passing. | One way to do "anonymous task" — the inline prompt |
| **✅ Shipped** (Apr 19, strategy doc) | `docs/strategy.md` written as the definitive source of truth for how cam-flow works (8 sections: agent mgmt, context, node exec, DSL, plan/execute boundary, preflight principle, inline→agent→skill promotion, CLI reference). `docs/architecture.md` trimmed to a code-level companion with pointers back to strategy. `docs/cam-phase-plan.md` marked HISTORICAL. | One entry point for "how does cam-flow work?" — reduces doc drift |
| **This week** | §5.1 context positioning (HQ.1) — prompt reorder | Better agent engagement with CONTEXT |
| **Week 2** | §5.2 observation masking (HQ.2) · §6.1–6.2 checkpoint local mode (CP.1–2) | Efficient long loops + git safety net |
| **Week 3** | §5.3 tool scoping (HQ.3) · §5.4 multi-layer verification (HQ.4) · §6.2 branch/remote checkpoint modes | Higher fix success rate |
| **Week 4** | §4.1 methodology router · §4.2 escalation ladder | Smarter failure handling |
| **Week 5–6** | §7.1 skill evolution Phase 1 · §7.2 port 3 hermes skills · §7.3 iteration budget · §7.4 dry-run polish | Measurement + hardened skills + safety rails |
| **Month 2** | §3.3 camc session ID (camc team) · §3.6 RTL artifact refs + examples | Hardware verification workflows feasible |
| **Month 3+** | §8 items 10–14 | Parallel execution, SDK phase, advanced evolution, webhook, Hermes adapter |

---

## 11. Open questions

1. **Checkpoint mode default.** `local` (no remote) is safest, but
   teams sharing a repo may want `branch` as their default. Possibly
   config via `~/.cam/config.yaml` so the user picks once.
2. **Observation masking threshold.** When should full output fold
   into the summary history? After 1 round? 2? Worth A/B-testing on
   the calculator demo.
3. **Tool scoping granularity.** Should cam-flow ship a curated
   `tool_profile` vocabulary (e.g. `readonly`, `editor`, `search`)
   so users don't have to enumerate tool names? Convenience vs
   leakage risk.
4. **State schema migration.** Existing `state.json` files from CLI
   phase runs need migration to the six-section schema. Ship a
   one-shot converter in `camflow migrate`.
5. **Skill evolution ownership.** Reports could live alongside state
   (`.camflow/`) or centrally (`~/.cam/evolve/`). Likely: per-project
   writes, central read-side aggregator.
6. **Hermes skill licensing.** Apache-2.0. Attribution in ported file
   headers is enough.

---

## 12. Document history

- 2026-04-18 — Initial version after CAM Phase engine shipped and Hermes investigation concluded.
- 2026-04-18 — Added Exception Handler section (methodology router, escalation ladder, precompact state preservation). Inspired by PUA project; engineering only, no pressure rhetoric.
- 2026-04-18 — Adopted stateless execution (Option C). §3.1 rewritten as shipped: state_enricher + six-section state + fenced CONTEXT injection. PreCompact State Preservation descoped.
- 2026-04-18 — Calculator demo shipped (first end-to-end CAM run): fixed `--auto-exit` reliance (removed), added `_kick_prompt` to submit pasted prompts, changed `camc rm --force` → `--kill` (camc CLI rename), repaired `cli_entry/main.py`. 4 fix agents completed in ~15 s each; all 11 calculator tests pass.
- 2026-04-18 — Major restructure: added §1 Design Principles (7 principles from Pachaar's "Anatomy of an Agent Harness"), §5 Harness Quality Improvements (context positioning, observation masking, tool scoping, multi-layer verification), §6 Checkpoint System (git-based, 3 storage modes, history/restore). Timeline rewritten to week-by-week plan.
