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

### 29. tmux Lifecycle on rm — `camc rm` must kill tmux + socket

- **What.** When `camc rm <id>` removes an agent from the registry,
  it must ALSO terminate the tmux session and unlink the tmux
  socket. Otherwise camc's heal/adoption logic will resurrect the
  "deleted" agent on the next monitor cycle, and `camc list` will
  re-show it with a fresh internal record.
- **Why.** This is the actual root cause of the "leftover agents"
  problem we previously misdiagnosed as a workdir-uniqueness issue.
  Even the cleanup hardening shipped in cam-flow (PATH resolution,
  try/finally, sweep, kill-before-start) is defeated if
  `camc rm` itself returns 0 but leaves the tmux session alive —
  heal then re-adopts it as if it were a new agent and the next
  `cleanup_all_camflow_agents()` finds it again. A leak we cannot
  close from the cam-flow side.
- **Symptoms observed.**
  - 6 dead `camflow-fix` tmux sessions surviving after a
    calculator-demo run, even though the engine's per-node
    cleanup ran. They came BACK after every sweep.
  - On reboot, agents like `l1tcm`, `eab4f56e`, `camflow` got
    re-adopted under each other's IDs (originally chalked up to
    workdir collisions; the real issue is heal seeing zombie tmux
    sessions and binding new IDs to them).
- **Fix scope (camc-side).**
  1. `camc rm <id>` must always run `tmux kill-session -t <session>`
     before deleting the registry entry, even if `--kill` was not
     passed (or make `--kill` the default).
  2. After `kill-session`, the `/tmp/cam-sockets/cam-<id>.sock` file
     must be unlinked. Stale sockets on disk also confuse heal.
  3. heal's adoption pass should reject any tmux session whose
     registry entry was removed — currently it adopts orphan tmux
     sessions blindly, which is what re-creates the deleted agents.
- **Workdir uniqueness — superseded.** The earlier theory (multiple
  agents sharing `/home/hren` as workdir cause session-ID
  collisions) was a symptom, not a cause. The collision is between
  zombie tmux sessions and the registry, not between workdirs.
  Workdir uniqueness might still be a useful camc lint / warning
  for other reasons, but it would not have prevented bug #10.
- **Source.** Diagnosis from `camflow-cleanup-fix.md` task +
  observation that agents reappear after `cleanup_all_camflow_agents`
  sweeps. Updated 2026-04-18.
- **Status.** Idea — camc-side fix. cam-flow's belt-and-suspenders
  cleanup is necessary but insufficient; we also need camc to honor
  the contract that `rm` means dead.

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

### 33. `camflow plan` — NL → workflow.yaml generator

- **What.** One-shot planner CLI that turns a natural-language
  request into a validated `workflow.yaml`. Collects CLAUDE.md +
  skills/ + environment context, builds a prompt with 3 few-shot
  examples, calls a strong model once, validates both DSL
  correctness and plan quality (orphans, loops without retry cap,
  agents missing verify / methodology / allowed_tools, un-sourced
  `{{state.x}}` refs), writes the YAML, prints an ASCII graph.
- **Why.** Writing workflow.yaml by hand is slow and drifts from
  best-practice conventions. The planner bakes those conventions
  in. Pluggable LLM backend (anthropic SDK preferred, claude CLI
  fallback) keeps it usable even without an API key.
- **Source.** Task `camflow-planner-impl.md` (2026-04-19).
- **Status.** SHIPPED 2026-04-19. See `src/camflow/planner/`,
  `src/camflow/cli_entry/plan.py`, `tests/unit/test_planner.py`.

### 34. Replan on failure (planner Phase 2)

- **What.** When a workflow fails at L4 escalation (or hits a
  workflow-level timeout), bundle the diagnostic state and feed it
  back to `camflow plan` as input context. The planner produces a
  revised workflow — maybe with different decomposition, stricter
  verify conditions, or a fallback path.
- **Why.** Today a failed workflow stops dead. The operator
  re-plans manually. The infrastructure is already there (planner
  accepts `claude_md_path` and `env_info` context) — we just need
  to wire a "here's the failing trace + final state" section into
  the prompt.
- **Source.** Natural extension once planner shipped.
- **Status.** Idea — planner Phase 2.

### 35. Planner A/B testing via trace rollup

- **What.** Measure whether different planner prompt variants
  produce workflows that complete more reliably. Trace rollup
  (`camflow evolve report`) as fitness signal: success rate, mean
  iterations to done, verify-failure rate.
- **Why.** The planner is high-leverage — small prompt
  improvements compound across every future workflow. Current
  baseline is "looks reasonable" which isn't a metric.
- **Source.** `docs/evaluation.md` §3.2 A/B experiments.
- **Status.** Idea — planner Phase 2.

### 32. Plan vs Runtime boundary (plan-priority + verify cmd)

- **What.** Node-level fields in `workflow.yaml` (`methodology`,
  `escalation_max`, `max_retries`, `verify`, `allowed_tools`,
  `timeout`) take priority over runtime heuristics. Keyword-based
  methodology routing and default retry budgets become fallbacks
  that kick in only when the plan is silent. Plus: a new `verify`
  field runs a cmd after an agent's claimed success and downgrades
  the result to fail on non-zero exit, giving workflow authors a
  programmatic gate on agent claims.
- **Why.** Workflow authors often know better than the runtime
  which methodology a node needs, how many retries to allow, what
  the pass criterion actually is. Runtime heuristics (keyword
  router, default retry=3, agent self-report = source of truth)
  are good first-order defaults but shouldn't override an explicit
  plan. And agent self-reports of success are not proof —
  `verify: "pytest -k foo"` is proof.
- **Source.** RV32 ECC verification run revealed the gap: the
  plan wanted specific methodology + success criteria, but runtime
  had the last word. Pattern is standard in CI systems (Jenkins
  `post { success { sh './verify.sh' } }`, GitHub Actions
  `verify-signed-commits`).
- **Status.** SHIPPED 2026-04-18. See
  `src/camflow/engine/dsl.py` for the new `NODE_FIELDS`,
  `prompt_builder.build_prompt` for plan-first methodology/
  escalation injection, `backend/cam/engine.py::_apply_verify_cmd`
  for the verify gate, `tests/unit/test_plan_priority.py` for the
  14 new tests.

### 36. Preflight field — fail fast before expensive nodes

- **What.** New `preflight: <cmd>` field in the node DSL, peer to
  `verify:`. Engine runs preflight (cheap, seconds) BEFORE the node
  body; on non-zero exit, node immediately transitions as fail with
  `error.code = PREFLIGHT_FAIL`, skipping the expensive body
  entirely. Two-layer validation: preflight answers "can I even
  start?"; verify answers "did I succeed?".
- **Why.** Production discovery — CoreMark ran a 30-min simulation
  before we found out the core never booted after reset. Expensive
  nodes (simv runs, formal verification, tree builds) can take
  tens of minutes; running them when a prerequisite is obviously
  missing wastes a full retry budget and delays failure reporting.
  Preflight turns those silent hour-long wastes into second-long
  early fails with an explicit reason code.
- **Source.** `docs/lessons-2026-04-19.md` §P2. Confirmed as a
  standing architectural decision in §3.6 of that doc.
- **Status.** SHIPPED 2026-04-19. `NODE_FIELDS` in
  `engine/dsl.py` now accepts `preflight`; engine.py has
  `_run_preflight` running BEFORE every node body (shell or agent)
  with a 60 s timeout; `PREFLIGHT_FAIL` / `PREFLIGHT_TIMEOUT` /
  `PREFLIGHT_ERROR` error codes; planner prompt embeds the
  "> 5 min → preflight" rule plus a preflight cookbook.
  Tests: `tests/unit/test_preflight.py` covers direct, dispatcher,
  template substitution, timeout, and exception paths.

### 37. Agent definition system (~/.claude/agents/ reuse)

- **What.** Replace the ad-hoc `do: agent claude` form (anonymous
  tool call, prompt written from scratch in every workflow.yaml)
  with `do: agent <name>` referencing a predefined agent at
  `~/.claude/agents/<name>.md`. Each agent definition carries:
  name, description, model, allowed tools, preloaded skills, and
  a system prompt. The workflow.yaml says WHICH agent + WHAT task;
  the agent file owns identity, capabilities, and style.
- **Why.** Right now every node writes its own prompt. No reuse,
  no quality control, no skill-loading reuse. You can't say "use
  the hardware-verification agent" and get a consistent setup
  across workflows. Moving the agent identity out of workflow.yaml
  into a reusable file also lets the Planner pick an agent from a
  registry (Phase 2 COLLECT) instead of hand-crafting a prompt.
  Claude Code already has an agent-definition convention at
  `~/.claude/agents/`; cam-flow should consume it.
- **Source.** `docs/lessons-2026-04-19.md` §P1 + §TODO "Agent
  definition system." Pattern aligns with Claude Code's existing
  agent system and with camflow-manager's COLLECT phase already
  listing `~/.claude/agents/`.
- **Status.** SHIPPED 2026-04-19. New module
  `backend/cam/agent_loader.py` parses
  `~/.claude/agents/<name>.md` frontmatter (name, description, model,
  tools, skills) and body (system prompt). DSL `classify_do` routes
  `agent <name>` through the loader; `prompt_builder.build_prompt`
  takes an `agent_def=` kwarg and injects the persona + role line.
  Planner prompt now includes an Agent catalog section (pulled via
  `list_available_agents()`). Model override support is wired through
  `NODE_FIELDS += 'model'` but not yet enforced against camc (camc
  has no `--model` flag today) — future promotion to hard-enforce.

### 38. Domain-specific planner rule sets

- **What.** Let the Planner load an additional rule pack based on
  workflow domain — e.g. `--domain hardware`, `--domain software`,
  `--domain deployment`, `--domain research`. Each rule pack
  augments the planner prompt with domain-aware conventions:
  analyze-DUT-before-build for hardware RTL, validate-service-alive
  for deployment, test-first for software, etc. Deterministic
  operations per domain (smake/VCS/JasperGold for hardware;
  pytest/npm for software) get flagged as `cmd` not `agent`.
- **Why.** The current planner prompt is generic. Plans frequently
  miss critical domain steps: hardware plans forget to analyze the
  DUT interface before writing TB, benchmark plans skip
  validate-DUT-alive before running tests, memory-bound software
  plans miss prereq install. The agent then has to discover what
  the planner should have planned — usually after burning time.
  Packaging domain knowledge as a rule pack (pluggable, composable)
  is cheaper than retraining the planner and cleaner than bloating
  the generic prompt with every domain's conventions.
- **Source.** `docs/lessons-2026-04-19.md` §P4 + §TODO
  "Domain-specific planner rules."
- **Status.** SHIPPED 2026-04-19. `DOMAIN_PACKS` dict in
  `planner/prompt_template.py` with `hardware`, `software`,
  `deployment`, `research` packs; `--domain` flag on
  `camflow plan` (choices-restricted in argparse); `generate_workflow`
  takes `domain=` kwarg and passes through. Unknown domains fall
  through silently. Tests in `test_planner.py` cover injection,
  hardware content, unknown-domain noop.

### 39. Inline-prompt node type (anonymous default agent)

- **What.** A node whose `do` field has no keyword prefix is treated
  as a free-text prompt to the default agent:
  `do: "Fix the bug in calculator.py"`. No `with`, no agent
  definition — just write the instruction and go.
- **Why.** For trivial one-off tasks, ceremony (pick an agent name,
  split `do` and `with`) is noise. Inline prompts make the DSL as
  terse as the task merits. Named agents remain preferred when the
  persona / tool scope / skill list actually matters.
- **Source.** `camflow-dsl-update.md` priority 1.
- **Status.** SHIPPED 2026-04-19. `classify_do` returns
  `("inline", body)` for free-text. `build_prompt(inline_task=body)`
  routes the text as the task body. Tests:
  `test_node_runner_dispatch.py::test_dispatch_inline_prompt`.

### 40. Rename `cmd` → `shell` (keep `cmd` as alias)

- **What.** Shell command nodes now spell as `shell <command>`. The
  old `cmd <command>` form is still accepted as a deprecated alias
  — existing workflows continue to work unchanged.
- **Why.** "cmd" is ambiguous (Command? cmd.exe? Command pattern?).
  "shell" says exactly what it is. The planner prompt, docs, and
  examples now prefer `shell`, and the planner emits `shell` for
  new plans.
- **Source.** `camflow-dsl-update.md` priority 1.
- **Status.** SHIPPED 2026-04-19. `classify_do` folds `cmd` into
  `shell`. Validator, node_runner, engine.py, planner prompt, and
  docs all use `shell` as the canonical spelling. No migration
  needed for existing workflows.

### 41. Verify checks OUTCOME, not OUTPUT (planner rule)

- **What.** Planner prompt rule: verify conditions must check the
  OUTCOME the node was supposed to achieve, not the OUTPUT of the
  command that ran. `test -f simv` proves the binary exists but not
  that the simulation ran. `grep -q finish simv.log` proves sim
  finished but not that the core executed instructions.
- **Why.** Production discovery: three separate RV32 ECC / CoreMark
  nodes passed verify but the real goal wasn't met. Weak verify is
  worse than no verify — it creates false confidence and lets bad
  runs propagate downstream.
- **Source.** `docs/lessons-2026-04-19.md` §P3 + §P6.
- **Status.** SHIPPED 2026-04-19. Planner prompt `PLANNING_RULES`
  (§3) and verify-condition cookbook now explicitly state "OUTCOME,
  not OUTPUT" with worked examples.

### 42. One node = one independently verifiable deliverable (planner rule)

- **What.** Planner prompt rule: split any node that would need to do
  X, check, then do Y. Each node should produce exactly one thing you
  can prove with a single verify check.
- **Why.** Production discovery: `run_simv` tried to dump wave +
  analyze signals + fix boot + recompile + verify + run CoreMark.
  Timed out mid-work, no state preserved. Fine-grained nodes each
  with their own verify make progress durable against timeouts.
- **Source.** `docs/lessons-2026-04-19.md` §P7.
- **Status.** SHIPPED 2026-04-19. Planner prompt `PLANNING_RULES`
  (§2) now embeds this rule verbatim.

### 43. Lesson feedback to planner (auto-evolve planner rules)

- **What.** After a workflow completes, rollup `.camflow/trace.log`
  for patterns ("node X failed N times because Y") and promote
  recurring patterns into planner rules — either appended to
  `PLANNING_RULES` or injected as an extra context block on the next
  plan call. Closes the loop between execution discoveries and
  planning.
- **Why.** Right now each production run surfaces new planner rules
  (verify-outcome, one-node-one-deliverable, preflight) and a human
  has to hand-edit the prompt. An automated feedback loop compounds
  every workflow's learning into the next plan without a human in
  the middle.
- **Source.** `docs/lessons-2026-04-19.md` §P5 medium-term TODO.
- **Status.** Idea — medium-term. Requires: trace summarization
  (`camflow evolve report` already exists), a pattern detector, and
  a persistent "planner rulebook" file. Groundwork exists in
  `src/camflow/engine/memory.py` (trace rollup).

### 44. Planner skill-scout (read-only skill discovery)

- **What.** A read-only scout the Planner can call (Option B today,
  Option A later) to ground its choice of `skill <name>` nodes:
  `camflow scout --type skill --query "<capability>"` runs
  `skillm search` (falls back to walking `~/.claude/skills/`) and
  returns the top 5 matches with each SKILL.md's first lines. The
  Planner sees the JSON reports as a "Scout reports" section in its
  prompt and picks skills informed by the actual catalog instead of
  guessing.
- **Why.** Production discovery: planner-generated workflows
  hallucinated skills that didn't exist on the host. Hand-curated
  skill lists in CLAUDE.md drift fast and don't tell the planner
  which SKILL.md actually does what. Letting the planner ground its
  pick on a live `skillm search` (no LLM tokens spent on the search,
  just the structured report) makes plans match reality.
- **Source.** `camflow-planner-scout.md`.
- **Status.** SHIPPED 2026-04-19 (Option B: scout runs OUTSIDE the
  LLM call, results flow in via `--scout-report`). See
  `src/camflow/planner/scouts.py::run_skill_scout`,
  `src/camflow/cli_entry/scout.py`, and the planner-prompt "Scout
  reports" renderer. Option A (scouts as Anthropic SDK
  `tools=[...]` definitions, called mid-generation) remains future
  work — the scout function signature is already SDK-compatible.

### 45. Planner env-scout (read-only environment probe)

- **What.** A read-only scout for tool / path availability:
  `camflow scout --type env --query vcs --query smake --query p4`
  runs `which` + `--version` for each tool. `path:<abs>` probes
  filesystem existence. Returns a structured JSON report the
  Planner reads as additional context.
- **Why.** Production discovery: planner generated a `shell vcs ...`
  node on a host without VCS. Engine couldn't tell why preflight was
  even needed. A second-long `which` probe at plan time prevents
  hour-long sim runs that were never going to start. Also lets the
  planner choose between alternative tools (e.g. JasperGold vs
  formality) based on what's actually installed.
- **Source.** `camflow-planner-scout.md`.
- **Status.** SHIPPED 2026-04-19 alongside #44. See
  `src/camflow/planner/scouts.py::run_env_scout`.

### 46. Inline → agent → skill promotion guideline

- **What.** Documented decision rules for when an inline prompt
  should be promoted to a named agent definition (`~/.claude/agents/
  <name>.md`) and when an agent should be further promoted to a
  skill (`~/.claude/skills/<name>/SKILL.md`). Three-row decision
  table + ordered promotion rules in
  `docs/architecture.md` ("DSL v2: inline → agent → skill
  promotion"). The Planner uses the same rule when generating
  workflows.
- **Why.** DSL v2 gave authors three ways to express "AI does this"
  but no guidance on which to use. Ad-hoc choice → inline prompts
  proliferate (un-measurable, un-reusable) OR every prompt gets
  promoted to a skill prematurely (overhead, indirection). A
  written rule turns this into a checklist.
- **Source.** Author + `camflow-planner-scout.md` follow-up
  ("record the inline-to-skill promotion guideline to docs").
- **Status.** SHIPPED 2026-04-19. The architecture doc section is
  the source of truth; the Planner prompt's node-types catalog
  references it implicitly via the "prefer scout-confirmed skill,
  else agent, else inline" preference order.

### 48. `camflow resume` subcommand

- **What.** Explicit resume CLI: `camflow resume <wf>` flips
  `failed/aborted/engine_error` back to `running` and retries the
  same pc; `--from <node>` jumps pc; `--retry` forces an opt-in
  flip for `done`/`waiting`. Preserves
  completed/lessons/trace/failed_approaches; resets `retry_counts`
  and `node_execution_count` for the resumed pc only.
- **Why.** Auto-resume (engine reading state.json on `camflow <wf>`)
  only handled the `running` case — a node that ended in `failed`
  was stuck. Production discovery: CoreMark workflow ended `done`
  with a suspect score; the user wanted to add a `validate_trace`
  node and re-run from there without losing the 9 prior nodes' state.
- **Source.** `camflow-p1-batch.md` priority 2.
- **Status.** SHIPPED 2026-04-19. See
  `src/camflow/cli_entry/resume.py`,
  `tests/unit/test_resume.py` (23 tests).

### 49. `bin/camflow` wrapper + PATH symlink

- **What.** Bash script that resolves the repo root from its own
  location (symlink-friendly via `readlink -f`), prepends `src/` to
  PYTHONPATH, exec's `camflow.cli_entry.main`. Symlinked into
  `~/bin/` so `camflow plan ...` / `camflow resume ...` /
  `camflow scout ...` Just Work without PYTHONPATH prefixes.
- **Why.** Friction reduction. The verbose
  `PYTHONPATH=/path/to/cam-flow/src python3 -m camflow.cli_entry.main`
  invocation appeared in every camflow-manager skill recipe and every
  user message. A wrapper kills the boilerplate without touching
  Python imports.
- **Source.** `camflow-wrapper.md`.
- **Status.** SHIPPED 2026-04-19. See `bin/camflow`.

### 50. Definitive strategy document (`docs/strategy.md`)

- **What.** A single strategy document covering the 8 architectural
  strategies that answer "how does cam-flow work?": agent management,
  context management, node execution, DSL reference, plan/execute
  boundary, preflight principle, inline→agent→skill promotion, and
  CLI reference. Other `docs/` files now defer to it as the source
  of truth; `architecture.md` becomes the code-level companion.
- **Why.** Before this doc, strategy was scattered across
  architecture.md (plan vs runtime, user-facing lifecycle, DSL
  promotion), roadmap.md (shipped features), cam-phase-plan.md (old
  design), and various CHANGELOG entries. New contributors had no
  canonical place to read "how does it work?" — and some docs had
  drifted out of sync with code (e.g. cam-phase-plan.md pre-dated
  DSL v2 entirely).
- **Source.** `camflow-strategy-docs.md` (2026-04-19).
- **Status.** SHIPPED 2026-04-19. See `docs/strategy.md`;
  `docs/architecture.md` trimmed to point at it;
  `docs/cam-phase-plan.md` marked HISTORICAL.

### 47. Planner scouts as Anthropic SDK tools (Option A)

- **What.** Re-host `run_skill_scout` and `run_env_scout` as
  `tools=[...]` definitions on the Anthropic SDK call so the Planner
  LLM can request scouts mid-generation rather than relying on the
  caller to pre-populate them. The scout function signatures are
  already compatible — only the LLM-call wiring changes.
- **Why.** Today (Option B) the camflow-manager has to guess WHICH
  scouts the Planner will need before it knows what plan the Planner
  will produce. Sometimes that means running scouts the Planner
  ignores; sometimes it means the Planner wants info nobody scouted
  for. Letting the Planner request scouts mid-generation eliminates
  both failure modes — at the cost of a tool-use loop in the SDK
  call.
- **Source.** `camflow-planner-scout.md` Option A. Listed there as
  "migrate to Option A later (SDK phase)."
- **Status.** Idea — SDK phase. Blocked on the SDK-mode work
  (#22 in this backlog). Once SDK mode lands, the conversion is
  ~50 lines: define the two tools, dispatch tool_use blocks to
  `default_scout_fn`, loop until `stop_reason != "tool_use"`.

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
