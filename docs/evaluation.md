# cam-flow Evaluation Framework

How we measure whether each cam-flow component is actually helping. A
feature that can't be measured can't be improved. This document pairs
with `docs/architecture.md` (which lists every component) by naming the
metric, the measurement method, and the baseline for each.

Philosophy: every workflow run already produces `trace.log` with per-step
data. Evaluation is an offline query over traces — no special test
harness, no instrumentation we didn't already have.

---

## 1. Component metrics

Components are listed in the order they matter for end-to-end workflow
health. Baselines are the current state (from the calculator demo and
the 155-test suite).

| # | Component | Metric | How to measure | Baseline |
|---|-----------|--------|----------------|----------|
| 1 | Six-section state (`state_enricher.py`) | Fix-agent success rate on first attempt | Integration test: run calculator demo with enricher vs. with `enrich_state = identity`; count attempts per bug | ~1 fix attempt per bug (current demo: 4 bugs, 4 fix agents) |
| 2 | Fenced recall (`prompt_builder.py`) | Agent follows task vs. chases context | Trace review: count `state_updates.detail` values that describe re-doing a completed action | Unmeasured — no re-do observed in 4-agent demo |
| 3 | Context positioning (HQ.1) | Fix success on first attempt | A/B across N runs: context-first prompt vs. context-middle prompt, same workflow | Unmeasured (current: context-middle) |
| 4 | Observation masking (HQ.2) | Prompt token count at iteration N | Track `prompt_tokens` in trace entries across a 10-iteration loop | Current: grows ~linearly with iterations (test_output + completed) |
| 5 | Tool scoping (HQ.3) | Irrelevant tool usage count per node | `tools_used / tools_available` ratio per trace entry | Unmeasured — all agents get all tools |
| 6 | Error classifier (`error_classifier.py`) | Retry-mode match rate | % of retries where the chosen mode produced a different outcome than a blind retry would have | Unmeasured — `retry_mode` is computed but not compared to counterfactual |
| 7 | Methodology router (§4.1) | Fix success by task type | A/B: with router vs. default prompt, grouped by `do` category | Not implemented |
| 8 | Escalation ladder (§4.2) | Recovery rate after L2+ | % of workflows that complete after reaching L2 escalation vs. those that would fail without | Not implemented |
| 9 | Git checkpoint (§6.1) | Resume success rate | Force-kill engine mid-run, resume, assert workflow completes | No checkpoints yet |
| 10 | Lessons accumulation (`memory.py`) | Repeat-mistake rate | Count of `state_updates.detail` values whose action matches a `failed_approaches` entry | Unmeasured |
| 11 | Agent lifecycle (`agent_runner.py`) | Agent cleanup success rate | After engine exits, count of leftover `camflow-*` agents in `camc list` | Target: 0 (validated in demo) |
| 12 | Atomic persistence (`persistence.py`) | State corruption on crash | SIGKILL during a `save_state_atomic` call, verify loaded state is either the old or new value (never partial) | Validated by unit test |
| 13 | Orphan handler (`orphan_handler.py`) | Duplicate agent rate on resume | Integration test: crash mid-node, resume, check only one agent touched the result file | Validated by resume test |

### 1.1 Workflow-level metrics

Beyond per-component, measure the whole system:

- **End-to-end success rate.** Fraction of workflow runs that reach
  `status == "done"`.
- **Mean steps to completion.** Median `iteration` field on
  done-terminal traces.
- **Mean agent duration.** Median `duration_ms` for
  `exec_mode == "camc"` rows.
- **Token efficiency.** Sum of `prompt_tokens` across the run;
  divide by `iteration`.
- **Cost per fix.** Token sum × price / number of fixes landed.

---

## 2. Data collection — trace field additions

Every trace entry is already rich (see `backend/cam/tracer.py`). To
enable the metrics above, add these optional fields. All default to
None / False / 0 so existing tests and tooling keep working.

| Field | Type | Meaning | Used by metric |
|-------|------|---------|----------------|
| `prompt_tokens` | int | Total tokens in the prompt sent to the agent | 4, 5 (HQ.2, HQ.3) |
| `context_tokens` | int | Tokens within the fenced CONTEXT block | 2, 3, 4 |
| `task_tokens` | int | Tokens in the task body + output contract | 3, 4 |
| `tools_available` | int | Number of tools the agent was allowed to use | 5 (HQ.3) |
| `tools_used` | int | Number of tools the agent actually called | 5 |
| `context_position` | str | `"first"` or `"middle"` — for A/B on HQ.1 | 3 |
| `enricher_enabled` | bool | Was state_enricher run (for A/B) | 1 |
| `fenced` | bool | Was the CONTEXT fence applied | 2 |
| `methodology` | str | `"rca"` / `"simplify-first"` / `"search-first"` / `"working-backwards"` / `"systematic-coverage"` / `"none"` | 7 |
| `escalation_level` | int | Current L0..L4 | 8 |

### 2.1 Token counting

Token counts are approximate but consistent. Use a single tokenizer
function in the engine (`_approx_token_count(s)`) that applies a fixed
heuristic:

```
tokens = max(1, len(text) // 4)
```

This under-counts code by ~10% and over-counts prose by ~5% but is
deterministic and zero-dependency. Upgrading to a real tokenizer
(tiktoken / anthropic tokenizer) is a one-liner once we decide the
tradeoff is worth it.

Total, context, and task counts are taken at prompt-build time in
`build_prompt` / `build_retry_prompt` and passed down to the trace.

### 2.2 Tool counts

- `tools_available`: currently the default tool set Claude Code
  launches with (8 tools: Bash, Edit, Read, Write, Glob, Grep,
  WebFetch, TodoWrite + NotebookEdit). Once `allowed_tools` lands
  (§HQ.3), this is `len(node.allowed_tools)`.
- `tools_used`: requires post-run parsing of the agent's screen capture
  (or a Claude Code hook that counts invocations). Deferred to HQ.3
  implementation.

### 2.3 Context position and enricher / fenced flags

Booleans and a string — cheap to record. Default values match current
behavior (`context_position="middle"`, `enricher_enabled=True`,
`fenced=True`).

---

## 3. Measurement plan

### 3.1 Offline analysis

All metrics are computed from `trace.log` files. A new CLI
(`cam evolve report`, §7.1 of roadmap) walks a directory of traces
and emits:

- Pass/fail rate per component
- Token efficiency trend
- Retry-mode accuracy
- Escalation-level distribution

Output: `~/.cam/evolve/report.json` + ASCII dashboard.

### 3.2 A/B experiments

Several metrics are comparative (HQ.1, HQ.2, HQ.3, methodology
router). The experiment harness is:

1. Pin a workflow + starting state.
2. Run N times with feature A, N times with feature B.
3. Record traces into
   `.camflow/experiments/<exp-id>/<variant>/run-<k>/`.
4. `cam evolve report --experiment <exp-id>` compares the variants
   on the target metric.

For HQ.1 context positioning: experiment is binary, target metric is
fix-success on first attempt, N = 10 runs per variant.

### 3.3 Regression guards

Some metrics are asserts, not experiments. They fail CI when they
degrade:

- `agent cleanup success rate == 100%` (assert no leftover
  `camflow-*` agents after integration tests).
- `state corruption on crash == 0` (verified by the atomic-write
  unit tests).
- `duplicate agent rate on resume == 0` (verified by the orphan
  resume test).

Integration tests enforce these every CI run.

---

## 4. Current baselines (2026-04-18)

From the calculator-demo run that shipped the first successful
end-to-end CAM execution:

- End-to-end success: 1 / 1 = 100% (single run).
- Steps to completion: 12 (start pytest × 2 retries + 4 fixes + 4
  inter-fix tests + final test + done cmd).
- Fix-agent mean duration: ~17 s (range: 14–20 s).
- Agent cleanup success: 100% (no leftovers after the run).
- Bugs per fix agent: 1 (exactly as instructed by CLAUDE.md).
- Final pytest: 11 passed, 0 failed.

Lessons-accumulated count in the final state: 0. The agents did not
register any `new_lesson` — the bugs were routine; the CLAUDE.md
instruction to record lessons was non-binding. This is a signal that
the lessons mechanism hasn't been exercised on a hard enough task
yet.

---

## 5. Evaluation checklist (what's missing to fully measure each §)

- [ ] Add token fields to trace entries (`tracer.py`). **Done in
      this commit** — fields default to None for back-compat.
- [ ] Wire token counting into `build_prompt` and
      `build_retry_prompt` (they return `(prompt, token_info)`).
- [ ] Wire tool-count extraction from camc capture into
      `finalize_agent`.
- [ ] Implement `cam evolve report` CLI.
- [ ] Define A/B experiment harness in `src/camflow/evolve/`.
- [ ] CI assertion for agent cleanup success rate.

Without these, evaluation is qualitative ("did the demo complete?").
With these, every component has an objective number we can track
across releases.

---

## 6. Document history

- 2026-04-18 — Initial version. Defined metrics table, trace fields,
  measurement plan. Added token-counting fields to `tracer.py` (see
  architecture.md §backend/cam/tracer.py).
