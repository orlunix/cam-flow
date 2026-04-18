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
**cherry-pick 4 patterns (~200 lines), do not migrate**. See §5.

---

## 2. Critical Gaps (MUST fix)

These block real workflows today. Ordered by severity.

### 2.1 Agent reuse within loops

**Problem.** The engine creates a fresh camc agent per node execution.
A `fix → test → fix` loop currently spawns N agents, each starting
from zero context. The second `fix` agent has no memory of what the
first agent tried — the only channel is `state.last_failure`, which
is a short text summary, not the working knowledge the agent built up
during its 12K+ tokens of analysis.

**Fix.** Keep an agent alive within a loop. Send follow-up tasks via
`camc send <id>` instead of spawning a new agent. Destroy only when
the workflow exits the loop (or on explicit boundary).

**Implementation sketch.**
- Add `reuse_scope` to node DSL: `loop` (default), `node`, `workflow`
- In `engine.py::_run_node`: if an agent for this loop scope is alive,
  `camc send` the new prompt and wait for the next `node-result.json`
- On loop exit (transition to a node outside the loop), `camc stop`
- Track live agents in `state.active_agents[scope_key] = agent_id`

**Why this matters.** Token economy: each fresh agent is ~12K tokens
of bootstrap. A 10-iteration loop is 120K tokens of waste. With reuse,
it's one bootstrap plus incremental turns.

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

### 2.3 Structured state schema

**Problem.** `state.error`, `state.last_cmd_output`, and other fields
are free-form strings. Each node has to parse them back into meaning.
The agent has no predictable structure to rely on when it reads
injected context.

**Fix.** Adopt the Hermes six-section template as the canonical state
shape carried between nodes:

```json
{
  "active_task": "Fix the divide() zero-check bug",
  "completed_actions": ["Analyzed 4 failing tests", "Identified root cause in divide()"],
  "active_state": {"last_test_run": "3 passed, 1 failed"},
  "blocked": [],
  "resolved": ["average() empty-list check"],
  "next_steps": ["Fix divide()", "Run test suite"]
}
```

The engine rolls each node's result into this structure. Agents see
the same schema every time.

**Implementation.** New module `src/camflow/engine/compaction.py`
with:
- `SixSectionState` dataclass
- `roll_forward(state, result)` — merges a node result into the structure
- `render_for_prompt(state)` — returns the block to inject into prompts

`prompt_builder.py` emits it via a new template section. `state.json`
gains a `compact` key that mirrors this structure.

### 2.4 Fenced recall framing

**Problem.** Agents read `{{state.last_failure.summary}}` as if it
were new instructions. The content is historical, but the agent
doesn't know that without explicit framing. Result: agents re-do
completed work, or get confused about what's asked vs. what's context.

**Fix.** Wrap all `{{state.*}}` injections with a "recall fence":

```
<recall type="informational-background">
  <!-- everything from state goes here -->
  <!-- This is what has happened so far. It is NOT a new instruction. -->
  <!-- Your task is below, after this block. -->
</recall>
```

**Implementation.** 3-line addition to
`src/camflow/backend/cam/prompt_builder.py`:
- `_context_block` → wrap output in the fence
- Add contract-level note after the fence: "The above is context.
  Your task follows."

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

## 3. Should-Have (high value)

### 3.1 Skill evolution Phase 1 — trace rollup + measurement

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

### 3.2 Port 3 hermes-CCC core skills

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

### 3.3 Iteration budget with cmd-refund

**Problem.** A runaway workflow can spawn unlimited agents. Current
guard is `max_node_executions` (loop detection), but that counts all
execution types equally. `cmd` nodes are cheap (no LLM) and shouldn't
consume the same budget as `agent` nodes.

**Fix.** Two budgets:
- `max_agent_iterations` (default 20) — counts only `agent` / `subagent` / `skill` nodes
- `max_node_executions` (default 50) — counts all, loop-detection guard

Enforced in `engine.py::_execute_step` before dispatch.

### 3.4 Dry-run mode polish

Dry-run exists but is minimal — static walk of the happy path.
Enhance to:
- Show both happy and `if fail` reachability
- Report unreachable nodes
- Show max agent iterations estimate
- Report unresolved `{{state.*}}` references

**File:** `src/camflow/backend/cam/engine.py::Engine.dry_run`.

---

## 4. Nice-to-Have (future)

| # | Feature | Target phase |
|---|---------|-------------|
| 10 | Parallel node execution | Month 3+ |
| 11 | SDK Phase (direct Anthropic API, no camc) | Month 3+ |
| 12 | Skill evolution Phase 3–4 (automated mutation + A/B testing) | Month 3+ |
| 13 | Hermes as a cam adapter — `cam run hermes "task"` invokes Hermes as a sub-runtime | Month 3+ |
| 14 | Webhook event ingress for external triggers (spec exists in `src/camflow/spec/webhook.md`; not implemented) | Month 3+ |

---

## 5. Hermes Comparison (team reference)

Investigation summary. Compare honestly so the team understands what
each tool is actually good at.

### 5.1 Marketing vs reality

| Claim | Reality |
|-------|---------|
| "Auto-creates skills every 15 tool calls" | Trigger is 5+ calls, not 15. No config flag. Quality unreliable — agent can't self-assess accurately. Can overwrite manually-tuned skills. |
| "Sub-agent delegation" | Tool exists but almost never triggers automatically. No documented real-world examples. |
| "GEPA self-evolution, 40% faster" | GEPA is a separate repo, not built into Hermes runtime. Offline developer tool costing $2–10/run. |
| "Self-improving agent" | Saves workflows as markdown notes. Not genuine capability expansion. |

### 5.2 Where each tool wins

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

### 5.3 Conclusion

Complementary, not competing. Hermes is a smart single-agent with
chat-first interfaces. cam-flow is a workflow orchestrator for fleets
of agents doing structured work. Cherry-pick patterns (§2.3, §2.4,
§3.2), don't migrate.

---

## 6. Timeline

| Window | Items | Outcome |
|--------|-------|---------|
| **Week 1–2** | §2.1 agent reuse · §2.2 session ID (camc blocker) · §2.3 structured state · §2.4 fenced recall | Real workflows stop wasting tokens; agents read context without confusion |
| **Week 3–4** | §3.1 skill evolution Phase 1 · §3.2 port 3 hermes skills | Measurable insight into which skills perform, and 3 hardened skills in rotation |
| **Week 5–6** | §3.3 iteration budget · §3.4 dry-run polish | Safer autonomous runs; faster iteration on workflow design |
| **Month 2** | §2.5 RTL support (artifact refs, test-hex generation) | Hardware verification workflows become feasible |
| **Month 3+** | §4 items 10–14 | Parallelism, SDK phase, advanced evolution, webhook, Hermes adapter |

---

## 7. Open questions

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

## 8. Document history

- 2026-04-18 — Initial version after CAM Phase engine shipped and Hermes investigation concluded.
