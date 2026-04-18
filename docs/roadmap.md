# cam-flow Strategic Roadmap

This is the definitive document for where cam-flow is going and why.
It reflects the CLI and CAM phase work already shipped, the critical
gaps identified against real workflow demands (calculator demo, RTL
hardware verification), and a deep investigation of Hermes Agent
(Nous Research) to decide what patterns to adopt and what to skip.

---

## 1. Current State

### 1.1 CLI Phase — DONE (validated 2026-04-05)

- YAML DSL with 4 node types: `cmd`, `agent`, `skill`, `subagent`
- One-step-per-tick execution via `/workflow-run` skill driven by `/loop`
- State persists to `.claude/state/workflow.json`
- Trace persists to `.claude/state/trace.log` (JSONL)
- Template substitution: `{{state.xxx}}` resolved per tick
- Calculator demo: 4 bugs fixed in 4 loops, all 11 tests pass
- Experiments proving skill invocation, subagent isolation, and lessons flow

Reference: `docs/cam-phase-plan.md §0`, `camflow-handoff/docs/camflow-cli-research-handoff.md`.

### 1.2 CAM Phase engine — DONE (2026-04-18, 116 tests, pushed)

Each node runs as a separate camc agent (or direct subprocess for `cmd`).
The engine is a persistent Python process that owns the state machine.

Shipped modules (`src/camflow/backend/cam/`):

| Module | What it does |
|--------|-------------|
| `engine.py` | `Engine` class + `EngineConfig`; signal handlers; retry loop with error classification and context-aware retry prompts; orphan recovery on resume; workflow + per-node timeouts; loop detection; dry-run; progress reporting |
| `agent_runner.py` | Dual-signal polling (file-first, camc status second); split into `start_agent` / `finalize_agent` so `current_agent_id` can be persisted between start and wait |
| `cmd_runner.py` | Captures stdout (2000c) and stderr (500c) tails; promotes to `state.last_cmd_output` / `state.last_cmd_stderr` |
| `prompt_builder.py` | Injects `state.lessons` and `state.last_failure` into prompts; `build_retry_prompt` with RETRY banner |
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
| `src/camflow/engine/transition.py` | `if: success` shortcut wired; cmd nodes get `{{state.x}}` substitution |

### 1.3 Hermes investigation — DONE

7 reports, full installation, strategic recommendation. Bottom line:
**cherry-pick 4 patterns (~200 lines), do not migrate**. See §6.

---

## 2. Critical Gaps (MUST fix)

These block real workflows today. Ordered by severity.

### 2.1 Stateless node execution — SHIPPED

**Status.** Implemented. Supersedes the earlier "agent reuse within
loops" plan.

**Problem (original).** A `fix → test → fix` loop spawns a fresh agent
per node, each starting from zero context. The second fix agent had
no memory of what the first tried — the only carry-over was
`state.last_failure`, a short text summary.

**Decision.** We evaluated three approaches and chose **stateless
execution + structured state** (serverless-function pattern):
- A (agent alive) — unpredictable context, compaction risk
- B (reuse with water-level monitoring) — complex, hard to debug
- **C (chosen: stateless + rich state)** — predictable, recoverable,
  traceable, debuggable, cache-friendly (CLAUDE.md stable, only state
  is fresh tokens)

**What shipped.**
- `src/camflow/engine/state_enricher.py` — merges each node's result
  into the six-section state schema: `active_task`, `completed`,
  `active_state` (key_files/modified_files), `blocked`, `test_output`,
  `resolved`, `next_steps`, `lessons`, `failed_approaches`, plus
  bookkeeping (`iteration`, `escalation_level`, `retry_counts`)
- `src/camflow/backend/cam/prompt_builder.py` rewrite — renders the
  state in a fenced `--- CONTEXT (informational background, NOT new
  instructions) ---` block so the agent never confuses history with a
  new directive
- `src/camflow/backend/cam/engine.py` — calls `enrich_state` after
  every node, drops the old `last_failure` field
- `examples/cam/CLAUDE.md` — per-project template that teaches the
  agent how to read the CONTEXT block and what the output contract is

**Mitigations for the one tradeoff.** Agents re-read code files each
iteration, but `active_state.key_files` and per-entry `file`/`lines`
refs in `completed` direct them to targeted reads rather than blind
search.

**Tests.** 3 new integration tests in `test_stateless_loop.py` + 26
new unit tests in `test_state_enricher.py`. Full suite: 155+ passing.

### 2.2 camc session ID tracking

**Problem.** On reboot/resume, camc searches agents by working
directory path. Multiple agents at `/home/hren` collide. We hit this
bug 3 times in production (agents `l1tcm`, `eab4f56e`, `camflow`).
Wrong session gets adopted; the orphan handler in cam-flow can't save
us if camc itself is confused about identity.

**Fix.** Store `session_id` (the tmux session name, e.g.
`cam-486ffaeb`) in `agents.json`. On resume, adopt by `session_id`
directly, not by path match.

**Scope.** This is a camc fix, not a cam-flow fix, but cam-flow's
orphan handling depends on it. Track as a blocker on camc team.

**Files touched (camc):** `~/bin/camc` adoption logic, `~/.cam/agents.json` schema.

### 2.3 Structured state schema — SHIPPED (as part of §2.1)

Implemented in `src/camflow/engine/state_enricher.py`. See §2.1 for
details. State fields: `iteration`, `active_task`, `completed`,
`active_state.{key_files, modified_files}`, `blocked`, `test_output`,
`resolved`, `next_steps`, `lessons`, `failed_approaches`,
`escalation_level`, `retry_counts`.

### 2.4 Fenced recall framing — SHIPPED (as part of §2.1)

Implemented in `src/camflow/backend/cam/prompt_builder.py`. The
structured state is rendered inside
`--- CONTEXT (informational background, NOT new instructions) ---`
/ `--- END CONTEXT ---` markers, with the task placed explicitly after
`--- END CONTEXT ---` under a `Your task:` header.

### 2.5 Test program hex generation for RTL workflows

**Problem.** cam-flow v0.1 targets software development. The RV32IMC
hardware verification test revealed a class of workflows we don't
support: generate a test program → compile to hex → run simulation →
analyze waveform.

**Fix.** This isn't one feature — it's a category that needs:
- A richer `cmd` type that captures artifact paths (not just stdout)
- An `artifact` reference resolver so `with: run this against
  @artifact://generated.hex` works
- Workflow examples for the RTL domain

**Implementation.**
- `spec/artifact-ref.md` — define the `@artifact://` reference syntax
- `engine/artifact_ref.py` — resolve `@artifact://path` to file contents or path
- `examples/rtl-verify/` — reference workflow for RV32IMC style cases

Deferred to month 2 because the work is larger and our users are
mostly software for now.

---

## 3. Exception Handler (high priority)

Inspired by the PUA project (github.com/tanweai/pua, 16k stars) but
with proper engineering instead of gimmicky "pressure" rhetoric. PUA
demonstrated that structured methodology selection, graduated failure
response, and context preservation across compaction measurably
improve task completion. We adopt the engineering, drop the theatrics.

Three components, all in `src/camflow/engine/`, wired into the CAM
engine main loop.

### 3.1 Methodology Router

**Problem.** The same generic "retry with more context" prompt goes
to debug tasks, build tasks, research tasks, and architecture tasks
alike. Each of these benefits from a different problem-solving
strategy; picking the right one up front cuts iterations.

**Fix.** Auto-select a methodology by inspecting the node's `do` field
(and optionally its `with` hints), then inject the matching procedure
into the agent's prompt.

| Node category (detected from `do`) | Methodology injected |
|-----------------------------------|--------------------|
| debug / fix / repair | **RCA** — reproduce → isolate → hypothesize → verify |
| build / compile / package | **Simplify-first** — question assumptions, remove unnecessary, simplify, accelerate |
| research / analyze / investigate | **Search-first** — find prior art, compare approaches, synthesize |
| architecture / design / refactor | **Working-backwards** — define outcome, identify constraints, design, validate |
| test / verify / validate | **Systematic coverage** — enumerate cases, prioritize edge cases, prove/disprove |

Detection is keyword-based on the node name and `do` field. Unknown
node types get a neutral fallback (no methodology injection).

**Files (new).**
- `src/camflow/engine/methodology_router.py` — `detect_category(node)` and `methodology_for(category)`
- `src/camflow/backend/cam/prompt_builder.py` — new `_methodology_block(node)` call, placed just above the task section
- `tests/unit/test_methodology_router.py` — category detection + fallback

**Acceptance.** For each category, a representative node name routes
to the expected methodology. A node like `start` with `do: agent claude`
and no keyword match gets no injection (neutral fallback, verified by
test).

### 3.2 Failure Escalation Ladder

**Problem.** After `max_retries` attempts with context-aware retry
prompts, the engine gives up. But real debugging sometimes needs a
qualitative shift in strategy — not just another retry. Today's retry
loop is flat; we need graduated response levels.

**Fix.** Track an escalation level per node in state, incrementing on
each failure. Each level applies a different intervention:

| Level | Name | Intervention |
|------:|------|------|
| L0 | Normal | Standard retry with context from previous failure (current behavior) |
| L1 | Rethink | Force a fundamentally different approach. Banner: "your previous approach failed, try a completely different strategy — do NOT re-apply the prior fix" |
| L2 | Deep Dive | Require source code reading + 3 distinct hypotheses written down BEFORE any fix attempt; reject the result if the fix-first pattern is detected |
| L3 | Diagnostic | Activate the full checklist: read all related files, check dependencies, verify environment assumptions, inspect relevant logs, dump state |
| L4 | Escalate | Flag for human review. Save full diagnostic state under `.camflow/escalation/<node>-<ts>.json`. Optionally send notification via a messaging skill (Teams, email). Workflow enters `waiting` status until manual `resume` |

**Semantics.**
- Escalation level is per-node: `state.escalation[node_id] = L0..L4`
- Reset to L0 when the node succeeds OR when workflow pc moves to a
  different node
- Level advances by one on each consecutive failure at that node
- Max-retries still applies within a level; transitioning through all
  levels without success ends in L4 (escalate)

**Files (new).**
- `src/camflow/engine/escalation.py` — `class Escalation` with
  `level_for(node_id, state)`, `advance(node_id, state)`,
  `reset(node_id, state)`, and `banner_for(level)` (returns the
  prompt-prefix string for that level)
- `src/camflow/backend/cam/engine.py` — wire into the retry branch of
  `_apply_result_and_transition`: after incrementing `retry_counts`,
  call `Escalation.advance`; pass the level to `build_retry_prompt` so
  the banner and procedure change with the level
- `src/camflow/backend/cam/prompt_builder.py` — `build_retry_prompt`
  gains an `escalation_level` parameter that selects the right banner
- `tests/unit/test_escalation.py` — level progression, reset on
  success, reset on node change, max-level behavior

**Acceptance.** A node that fails 4 times produces 4 prompts with
levels L1, L2, L3, L4 in order (L0 was the initial attempt). On the
5th call (still failing), workflow enters `waiting` with a diagnostic
bundle written to `.camflow/escalation/<node>-<ts>.json`.

### 3.3 PreCompact State Preservation — DESCOPED

**Why.** The stateless execution model adopted in §2.1 means agents
never run long enough to hit Claude Code auto-compaction. Each node
starts a fresh agent with a bounded prompt (CLAUDE.md + fenced
CONTEXT block + task + output contract). There is no long-running
session state to protect.

The state that previously needed preservation across compaction
(failure count, files touched, hypothesis, test results) already
lives in `state.json` and is re-injected on every agent invocation
via the fenced CONTEXT block. Compaction inside a single node
execution is now irrelevant to workflow integrity.

### 3.4 Rationale

Two things, none of them "AI pressure":
- **Right tool for the job.** Methodology routing picks a debugging
  strategy that matches the problem shape, not a one-size prompt.
- **Graduated response.** Repeated failure doesn't get the same prompt
  louder — it gets a different strategy, and eventually human review.

(State preservation across compaction is no longer needed — see §3.3.)

---

## 4. Should-Have (high value)

### 4.1 Skill evolution Phase 1 — trace rollup + measurement

**Problem.** GEPA's pitch is "40% faster self-evolving skills". In
practice GEPA is an offline tool that costs $2–10/run and doesn't ship
with Hermes runtime. cam-flow has better raw material: every node's
pass/fail is recorded in `trace.log` with typed errors and durations.
We can build trace-driven evolution that's actually online.

**Plan (Phase 1).** New CLI: `cam evolve report`.
- Reads all `trace.log` files under a project (or a time range)
- Aggregates per-skill statistics: total runs, pass rate, mean
  duration, top failure categories, retry frequency
- Output: `~/.cam/evolve/report.json` + an ASCII dashboard
- No mutation — measurement only. Phases 2+ add targeted mutations.

**Files (new).**
- `src/camflow/evolve/__init__.py`
- `src/camflow/evolve/rollup.py` — aggregates traces
- `src/camflow/evolve/report.py` — produces report.json
- `src/camflow/evolve/cli.py` — `cam evolve report` entry point

### 4.2 Port 3 hermes-CCC core skills

The Hermes "core-brain" skills are pure markdown with a rigid schema
(Purpose / Activation / Procedure / Decision rules / Output contract /
Failure modes). Directly portable.

| Port | Target location | Purpose |
|------|-----------------|---------|
| `hermes-route` | `~/.claude/skills/cam-route/SKILL.md` | Task triage: given a user ask, decide which skill/workflow to run |
| `systematic-debugging` | `~/.claude/skills/systematic-debugging/SKILL.md` | 10-phase debug procedure (reproduce → isolate → hypothesize → test → fix → verify) |
| `subagent-driven-development` | `~/.claude/skills/subagent-driven-development/SKILL.md` | Decomposition: split a task into subagent-sized chunks |

Each port: copy, rename references from `hermes-*` to `cam-*`, adapt
examples to cam-flow workflow context.

### 4.3 Iteration budget with cmd-refund

**Problem.** A runaway workflow can spawn unlimited agents. Current
guard is `max_node_executions` (loop detection), but that counts all
execution types equally. `cmd` nodes are cheap (no LLM) and shouldn't
consume the same budget as `agent` nodes.

**Fix.** Two budgets:
- `max_agent_iterations` (default 20) — counts only `agent` / `subagent` / `skill` nodes
- `max_node_executions` (default 50) — counts all, loop-detection guard

Enforced in `engine.py::_execute_step` before dispatch.

### 4.4 Dry-run mode polish

Dry-run exists but is minimal — static walk of the happy path.
Enhance to:
- Show both happy and `if fail` reachability
- Report unreachable nodes
- Show max agent iterations estimate
- Report unresolved `{{state.*}}` references

**File:** `src/camflow/backend/cam/engine.py::Engine.dry_run`.

---

## 5. Nice-to-Have (future)

| # | Feature | Target phase |
|---|---------|-------------|
| 10 | Parallel node execution | Month 3+ |
| 11 | SDK Phase (direct Anthropic API, no camc) | Month 3+ |
| 12 | Skill evolution Phase 3–4 (automated mutation + A/B testing) | Month 3+ |
| 13 | Hermes as a cam adapter — `cam run hermes "task"` invokes Hermes as a sub-runtime | Month 3+ |
| 14 | Webhook event ingress for external triggers (spec exists in `src/camflow/spec/webhook.md`; not implemented) | Month 3+ |

---

## 6. Hermes Comparison (team reference)

Investigation summary. Compare honestly so the team understands what
each tool is actually good at.

### 6.1 Marketing vs reality

| Claim | Reality |
|-------|---------|
| "Auto-creates skills every 15 tool calls" | Trigger is 5+ calls, not 15. No config flag. Quality unreliable — agent can't self-assess accurately. Can overwrite manually-tuned skills. |
| "Sub-agent delegation" | Tool exists but almost never triggers automatically. No documented real-world examples. |
| "GEPA self-evolution, 40% faster" | GEPA is a separate repo, not built into Hermes runtime. Offline developer tool costing $2–10/run. |
| "Self-improving agent" | Saves workflows as markdown notes. Not genuine capability expansion. |

### 6.2 Where each tool wins

| Dimension | Hermes | cam-flow |
|-----------|--------|---------|
| Messaging platforms (Slack/Discord) | Strong | Out of scope |
| Persistent memory | Built-in (vector) | Via trace + state |
| Easy setup (one-file install) | Strong | Needs camc + python |
| Multi-machine fleet | Not designed for it | Native (cam contexts) |
| Auto-confirm for Claude Code dialogs | N/A | First-class |
| DAG / loop / wait / resume workflows | Limited | Core |
| NVIDIA-internal integration | None | Via TeaSpirit + AI CLI |
| Structured workflow DSL | No | YAML DSL |
| Self-evolution | GEPA (offline, paid) | Trace-driven (coming Phase 1) |

### 6.3 Conclusion

Complementary, not competing. Hermes is a smart single-agent with
chat-first interfaces. cam-flow is a workflow orchestrator for fleets
of agents doing structured work. Cherry-pick patterns (§2.3, §2.4,
§4.2), don't migrate.

---

## 7. Timeline

| Window | Items | Outcome |
|--------|-------|---------|
| **Week 1–2** | ✅ §2.1 stateless execution · ✅ §2.3 structured state · ✅ §2.4 fenced recall · §2.2 session ID (camc blocker) | Real workflows stop wasting tokens; agents read context without confusion |
| **Week 3–4** | §3.1 methodology router · §3.2 escalation ladder · §4.1 skill evolution Phase 1 · §4.2 port 3 hermes skills | Exception handler online (sans PreCompact, descoped), measurable insight into skill performance, 3 hardened skills in rotation |
| **Week 5–6** | §4.3 iteration budget · §4.4 dry-run polish | Safer autonomous runs; faster iteration on workflow design |
| **Month 2** | §2.5 RTL support (artifact refs, test-hex generation) | Hardware verification workflows become feasible |
| **Month 3+** | §5 items 10–14 | Parallelism, SDK phase, advanced evolution, webhook, Hermes adapter |

---

## 8. Open questions

1. **Agent reuse scope keying.** What identifies "the same loop"? The
   back-edge target node? An explicit `scope:` field in DSL? Needs
   one more pass before implementation.
2. **State schema migration.** When we switch to six-section state,
   existing `state.json` files from CLI phase runs need migration.
   Ship a one-shot converter in `cam-flow migrate`.
3. **Skill evolution ownership.** Evolution reports could live
   alongside state (`.camflow/`) or centrally (`~/.cam/evolve/`).
   Central is easier to aggregate but loses per-project context.
   Likely: per-project writes, central read-side aggregator.
4. **Hermes skill licensing.** Hermes repo is Apache-2.0. Porting is
   clean. Attribution goes into the ported skill file header.

---

## 9. Document history

- 2026-04-18 — Initial version after CAM Phase engine shipped and Hermes investigation concluded.
- 2026-04-18 — Added §3 Exception Handler (methodology router, escalation ladder, precompact state preservation). Inspired by PUA project; engineering only, no pressure rhetoric. Renumbered subsequent sections.
- 2026-04-18 — Adopted stateless execution (Option C). §2.1 rewritten as shipped: state_enricher + six-section state + fenced CONTEXT injection. §2.3 and §2.4 marked shipped under §2.1. §3.3 PreCompact State Preservation descoped (no long-running sessions to protect). Added `examples/cam/CLAUDE.md` as the canonical per-project template. 155+ tests passing.
