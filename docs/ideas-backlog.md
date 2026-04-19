# cam-flow Ideas Backlog

Every good idea we discussed, whether implemented or not. Nothing gets
lost. New ideas get appended; implemented ideas get their status
updated in place.

For each idea: **what** it is, **why** it matters, **source** of the
idea, and **current status** (SHIPPED / implementing now / planned /
idea only / REJECTED).

---

## Category 1: Context & Prompt Quality

### 1. Context Positioning (Lost in the Middle)

- **What.** Move critical context to the start/end of the prompt, not
  the middle.
- **Why.** Stanford "Lost in the Middle" research: 30%+ accuracy drop
  for content in mid-window positions. LLMs attend most to prompt
  start and end.
- **Source.** Pachaar, "Anatomy of an Agent Harness" (April 2026),
  citing Stanford.
- **Status.** SHIPPED (this commit). `prompt_builder.build_prompt`
  now puts the fenced CONTEXT block first, then methodology /
  escalation hints, then role line, then task body.

### 2. Observation Masking

- **What.** Only keep the latest round's full test output; summarize
  older rounds as one-line history entries.
- **Why.** Prevents context bloat in long fix→test loops. 10 rounds
  × 3 KB each = 30 KB of repeated test output otherwise.
- **Source.** JetBrains Junie pattern.
- **Status.** SHIPPED (this commit). `state_enricher._capture_test_output`
  archives the previous `test_output` as a bounded `test_history`
  list (cap 10) before overwriting.

### 3. Fenced Recall Framing

- **What.** Wrap injected state with explicit "informational
  background, NOT new instructions" markers so the agent doesn't
  treat history as a new directive.
- **Why.** Without fencing, agents re-run completed actions or get
  confused about what's being asked.
- **Source.** Hermes agent pattern.
- **Status.** SHIPPED (2026-04-18). `FENCE_OPEN` / `FENCE_CLOSE` in
  `prompt_builder.py`.

### 4. Six-Section State Template

- **What.** Structure state as active_task / completed / active_state
  / blocked / resolved / next_steps / lessons / failed_approaches.
- **Why.** Predictable state shape = predictable prompt = fewer
  wasted iterations.
- **Source.** Hermes compaction template.
- **Status.** SHIPPED (2026-04-18). `engine/state_enricher.py`.

### 5. Co-evolution Awareness

- **What.** Claude Code is trained alongside its own tool set (Bash,
  Read, Edit, Write, Glob, Grep). Don't build custom alternatives.
- **Why.** Fighting the model's training data adds friction with no
  upside. Cooperate with its tool patterns instead.
- **Source.** Pachaar, "Anatomy of an Agent Harness."
- **Status.** Design principle — no code needed. Recorded in
  `docs/roadmap.md §1` (Design Principles).

### 6. Prompt Caching Optimization

- **What.** CLAUDE.md is stable; Anthropic caches it. Put changing
  content in state.json (re-injected each call). Cache hits amortize
  a big stable preamble.
- **Why.** Lower cost and latency per iteration.
- **Source.** Anthropic prompt-caching design.
- **Status.** Architectural decision, already in effect — CLAUDE.md
  in `examples/cam/` is stable across a workflow run; state is
  separate.

---

## Category 2: Agent Execution

### 7. Per-Node Tool Scoping

- **What.** Different nodes get different tool sets. `analyze`:
  Read / Glob / Grep / WebSearch. `fix`: Read / Edit / Write / Bash.
- **Why.** Vercel removed 80% of tools and got better results. Fewer
  choices, less distraction.
- **Source.** Pachaar, "Anatomy of an Agent Harness" (Vercel case
  study).
- **Status.** SHIPPED (this commit, soft enforcement via prompt).
  `allowed_tools` in node DSL renders a "Tools you may use" line.
  Hard enforcement (via `camc run --allowed-tools`) deferred until
  camc exposes the flag; the API is ready.

### 8. Agent Reuse Within Loops

- **What.** Keep the same agent alive across fix→test→fix iterations
  so context accumulates naturally.
- **Why discussed.** Token economy: a fresh agent is ~12K bootstrap
  tokens.
- **Status.** REJECTED in favor of stateless execution + structured
  state. Rationale: predictable context size, perfect resume, fully
  debuggable, no compaction risk. Recorded for reference.

### 9. Methodology Router

- **What.** Auto-select a problem-solving strategy by task type.
  debug → RCA, build → Simplify-first, research → Search-first,
  design → Working-backwards, test → Systematic-coverage.
- **Why.** Matching strategy to problem shape cuts iterations.
- **Source.** PUA project's methodology-routing concept (minus the
  "pressure" rhetoric).
- **Status.** SHIPPED (this commit). `engine/methodology_router.py`.

### 10. Failure Escalation Ladder (L0..L4)

- **What.** Graduated response to persistent failures:
  L0 Normal → L1 Rethink → L2 Deep Dive (3 hypotheses) →
  L3 Diagnostic (full checklist) → L4 Escalate to human.
- **Why.** Flat retry is wasteful past the budget; each level
  applies a qualitatively different intervention.
- **Source.** PUA project's pressure-escalation concept.
- **Status.** SHIPPED (this commit). `engine/escalation.py`.

### 11. Ralph Loop Pattern

- **What.** For long-running tasks spanning multiple context
  windows: Initializer Agent sets up environment + progress file;
  Coding Agent reads progress, picks next task, commits, writes
  summary. Filesystem provides continuity.
- **Why.** Decouples memory from session — any crash is recoverable
  by re-reading the progress file.
- **Source.** Anthropic's Claude Code architecture.
- **Status.** Idea — our stateless model is structurally similar;
  could be formalized as a workflow pattern in `examples/ralph/`.

### 12. Hook-based Agent Exit

- **What.** Use Claude Code hooks (PostToolUse, Stop) to detect when
  the agent has written `node-result.json` and trigger exit, instead
  of polling for the file from outside.
- **Why.** More reliable than idle detection, pushes responsibility
  to the right place.
- **Source.** Observation from the agent_runner debugging session.
- **Status.** Idea. Current solution (file-signal polling) works but
  hooks would be cleaner.

---

## Category 3: Verification & Quality

### 13. Multi-Layer Verification

- **What.** Layer verification: lint (ruff) → typecheck (mypy) →
  format check → tests. Each layer catches a different class of
  error before the expensive one runs.
- **Why.** Boris Cherny (Claude Code creator): verification improves
  quality 2–3× *per layer*.
- **Source.** Cherny, via Pachaar's article.
- **Status.** Planned as a workflow pattern in
  `examples/cam-verified/` + `docs/patterns/multi-layer-verification.md`
  (§5.4 in roadmap). No engine change required.

### 14. LLM-as-Judge Verification

- **What.** After the fix agent succeeds, a separate subagent
  evaluates whether the fix is semantically correct (not just
  test-passing).
- **Why.** Catches "tests pass but wrong solution" cases where the
  agent gamed the verifier.
- **Source.** Pachaar's article (verification loop design).
- **Status.** Idea only.

### 15. Skill Auto-Extraction from Traces

- **What.** After a successful workflow, analyze trace.log and
  extract reusable patterns as SKILL.md files.
- **Why.** Traces have real pass/fail signals; they're better
  training data than chat history mined with LLM-as-judge.
- **Source.** Hermes GEPA concept, adapted.
- **Status.** Planned — Phase 2 of `skill-evolution-plan.md`.

---

## Category 4: State & Persistence

### 16. Git-based Checkpoints

- **What.** Auto-commit after each successful fix node. Modes:
  local (default), branch (`camflow/<id>`), remote (push after each
  commit).
- **Why.** Git log reveals every step; `git revert` rolls back.
- **Source.** Anthropic's "Ralph Loop" pattern.
- **Status.** SHIPPED local mode (this commit). `engine/checkpoint.py`
  wired into the success branch of `engine._finish_step`. Branch +
  remote modes planned for Week 3.

### 17. Atomic State Writes

- **What.** Write state to `path.tmp`, fsync, `os.rename`, then
  fsync the parent directory.
- **Why.** SIGKILL mid-write would otherwise leave a truncated JSON.
- **Source.** Classic POSIX-durability pattern; applied after we
  hit corruption once in early testing.
- **Status.** SHIPPED (2026-04-18).
  `backend/persistence.save_state_atomic` /
  `append_trace_atomic`.

### 18. Trace-Driven Skill Evolution

- **What.** `trace.log` has structured pass/fail signals — better
  training data than chat history. Build trace rollup → scoring
  dashboard → manual skill improvement → automated mutation.
- **Why.** We have strictly more signal than Hermes does; leverage
  it.
- **Source.** Our own trace format + critique of Hermes GEPA.
- **Status.** Planned — 4 phases, full design in
  `skill-evolution-plan.md`. Phase 1 (rollup + dashboard) estimated
  at 3 weeks.

### 19. Iteration Budget with CMD Refund

- **What.** Max agent-node executions per workflow; cmd nodes
  (cheap, deterministic) don't count against the budget.
- **Why.** Prevents runaway workflows from spawning dozens of
  agents without capping cheap verifications.
- **Source.** Hermes iteration-budget pattern.
- **Status.** Planned — roadmap §7.3. Separate from
  `max_node_executions` (loop detection, counts all nodes).

---

## Category 5: Multi-Agent & Orchestration

### 20. Hermes as a CAM Adapter

- **What.** `cam run hermes "task"` spawns a Hermes agent managed
  by cam's fleet infrastructure.
- **Why.** Best of both: Hermes's chat UX + cam's multi-machine
  management.
- **Source.** Hermes investigation recommendation.
- **Status.** Idea, ~1 week to implement.

### 21. Parallel Node Execution

- **What.** Run independent nodes concurrently. Requires a
  path-overlap guard (parallel reads OK; parallel writes to
  overlapping paths disallowed).
- **Why.** Wall-clock speedup when the graph has real parallelism.
- **Source.** Hermes's path-overlap pattern.
- **Status.** Planned for Month 3+. Requires DSL change
  (`parallel: [nodeA, nodeB]`).

### 22. SDK Phase

- **What.** Direct Anthropic API calls, skipping camc / Claude Code
  entirely. No tmux, no auto-confirm, no idle detection.
- **Why.** Simpler and faster for production batch workloads. Loses
  access to Claude Code's tool ecosystem in exchange.
- **Source.** Three-phase plan (CLI → CAM → SDK).
- **Status.** Planned Month 3+.

### 23. Webhook Event Ingress

- **What.** External systems (CI, monitoring, Teams) trigger
  workflow transitions via HTTP webhook. "Test passed in CI →
  advance to deploy."
- **Why.** Unlocks event-driven workflows.
- **Source.** Our own `spec/webhook.md` (specified but not
  implemented).
- **Status.** Planned; spec complete.

### 24. Cross-Workflow State Sharing

- **What.** When workflow A produces an artifact that workflow B
  consumes, share via a state registry rather than files.
- **Why.** Decouples producers and consumers; enables federated
  workflows.
- **Source.** Internal design discussion.
- **Status.** Idea only.

---

## Category 6: Evaluation & Evolution

### 25. GEPA-style Skill Evolution

- **What.** Evolutionary optimization of skill files using trace
  data as fitness signal. 4-axis scoring: outcome 0.40, retry
  reduction 0.25, token efficiency 0.15, LLM-judge 0.20.
- **Why.** Automates the manual tuning loop on SKILL.md files.
- **Source.** Hermes GEPA, adapted.
- **Status.** Planned. Full design in `skill-evolution-plan.md`.
  Cost: $1–4 per skill per evolution run.

### 26. A/B Testing for Prompt Changes

- **What.** When we change prompt structure (context positioning,
  methodology, fencing), run both variants on the same tasks and
  compare trace metrics.
- **Why.** Quantifies whether a change actually helps rather than
  guessing from a single run.
- **Source.** Standard evaluation practice, applied to prompt
  engineering.
- **Status.** Framework SHIPPED — `docs/evaluation.md` defines
  trace fields (`context_position`, `enricher_enabled`, `fenced`,
  `methodology`) for A/B. Harness infrastructure planned in
  `src/camflow/evolve/`.

### 27. Harness Thickness Monitoring

- **What.** Track how much of agent success comes from harness
  logic vs model capability. As models improve, harness should
  thin out. If we add complexity but success rate stays flat, the
  complexity is waste.
- **Why.** Prevents harness rot. A pattern that was load-bearing
  last year may be friction today.
- **Source.** Pachaar's article ("Thinner harness is better").
- **Status.** Idea — needs baseline measurements first (the
  evaluation framework provides the data).

---

## Category 7: Infrastructure

### 28. camc Session ID from /proc/fd

- **What.** Extract Claude Code session ID from open file
  descriptors (new Claude versions keep
  `.claude/tasks/<UUID>/.lock` open).
- **Why.** Works for camc-run agents and new Claude versions that
  don't expose the session ID directly.
- **Source.** camc internal debugging.
- **Status.** SHIPPED in camc (Layer 1 of a 4-layer fallback). Not
  a cam-flow change, but cam-flow's orphan handling depends on it.

### 29. Workdir Uniqueness Enforcement

- **What.** Don't allow multiple agents to share
  `/home/hren` as workdir — enforce unique working directories.
- **Why.** Prevents session-ID collision on reboot (root cause of
  agents `l1tcm`, `eab4f56e`, `camflow` mix-ups).
- **Source.** Bug #10 repro work.
- **Status.** Idea — should be a camc lint or warning, not cam-flow.

### 30. TeaSpirit Integration for Notifications

- **What.** When escalation reaches L4, send a Teams notification
  via the messaging skill webhook; a human reviews the diagnostic
  bundle and decides next steps.
- **Why.** Closes the loop on the escalation ladder — L4 currently
  drops a bundle on disk but nobody gets paged.
- **Source.** Natural follow-up once §4.2 landed.
- **Status.** Idea — messaging skill exists; wiring is ~1 day of
  work.

### 31. Auto-SSH for VDI

- **What.** VDI SSH tunnels drop periodically. Use `autossh` with
  keepalive for persistent tunnels.
- **Why.** Developer ergonomics.
- **Source.** Developer annoyance.
- **Status.** Idea — user handles the VDI side.

---

## How to use this document

- **New ideas.** Append under the matching category. Use the same
  four-field structure (what / why / source / status).
- **Status updates.** Edit the existing entry in place. Keep the
  source and why so the chain of reasoning stays readable.
- **Promotions.** When an idea moves from "idea only" to "planned"
  or "SHIPPED," update the status and cross-reference the roadmap
  entry or commit.

This is the long-memory file. The roadmap (`docs/roadmap.md`) is the
working-memory file. Ideas live here forever; roadmap items live
there until done.
