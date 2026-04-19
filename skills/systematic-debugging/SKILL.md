---
name: systematic-debugging
description: Run a disciplined multi-phase debugging loop inside a cam-flow fix node. Activate when failures are ambiguous, a previous fix did not stick, the bug spans files, or a fix must be proved rather than guessed. The engine's methodology router selects this skill when the node's do/with text matches fix/debug/bug/error/repair keywords.
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  ported_from: hermes-CCC systematic-debugging (NousResearch Hermes Agent)
  adapted_for: cam-flow CAM-phase fix nodes
  category: engineering
  tags:
    - debugging
    - root-cause
    - rca
    - fix-node
    - methodology
  maturity: beta
---

# Systematic Debugging (cam-flow adaptation)

## Purpose

- Turn a vague failure into a proved root cause.
- Prevent guess-driven bug fixing inside a cam-flow fix node.
- Separate symptom collection from patching so every retry learns something.
- Leave a reproducible, reviewable explanation in `node-result.json`.

## When to activate

- The current node's methodology hint is `RCA` (from
  `engine/methodology_router.py`).
- The CONTEXT block shows a prior failure in `state.blocked` or
  `state.failed_approaches` and a previous approach did not work.
- The CONTEXT block shows `test_output` or `test_history` entries
  with unclear root cause.
- Escalation level is L1+ (`state.retry_counts[node_id] >= 1`).

## Procedure (10 phases)

1. **Stabilize the report.** Write the exact symptom in one sentence.
   Record expected vs. actual behavior. Do not edit code in this phase.
2. **Reproduce the failure.** Find the smallest reproducible path.
   Prefer an automated command (`pytest -k …`, `python -c …`) that
   the engine's test node would run. If no reproducer exists, build
   one before continuing.
3. **Narrow the surface area.** Identify likely subsystem boundaries;
   compare passing vs. failing paths; use `git diff --stat` or
   binary search to reduce scope.
4. **Instrument selectively.** Add temporary prints/logging only at
   branching decisions. Never spray logs everywhere.
5. **Generate hypotheses.** Produce at least 3 distinct causes. Rank
   by explanatory power and test cost. Write down what evidence
   would falsify each.
6. **Prove or kill hypotheses.** Run the smallest experiment that
   differentiates your top candidates. One high-signal experiment at
   a time.
7. **Patch the true cause.** Change only code implicated by evidence.
   Smallest change that restores the invariant. Do not bundle
   unrelated cleanup.
8. **Verify the fix.** Re-run the reproducer. Re-run nearby tests.
   Check negative cases too — you need the test node to go green,
   not just pass one case.
9. **Add regression protection.** Add or strengthen the test that
   would have caught this. Prefer deterministic tests.
10. **Save durable lessons.** If the root cause reflects a reusable
    pattern, emit `state_updates.new_lesson = "<insight>"` in
    `node-result.json` so future iterations benefit.

## Decision rules

- If you cannot reproduce the issue, do not claim to have fixed it.
- If two symptoms disagree, debug the disagreement first.
- If a bug appeared after a refactor, compare invariants, not just
  syntax.
- If instrumentation is growing large, your narrowing step failed —
  go back to phase 3.
- If the patch is broad, your hypothesis is still weak — go back to
  phase 5.

## Output contract (cam-flow)

Write `.camflow/node-result.json` with:

```json
{
  "status": "success",
  "summary": "Fixed <function>: <one-line patch description>",
  "state_updates": {
    "files_touched": ["calculator.py"],
    "lines": "L16-20",
    "detail": "added `if b == 0: raise ValueError(...)`",
    "resolved": "<bug name that was fixed>",
    "new_lesson": "<optional durable insight>"
  },
  "error": null
}
```

If the fix cannot land, return `status: "fail"` with a summary that
documents symptom / hypotheses tried / why each failed. The engine's
escalation ladder will promote the next retry.

## Failure modes

- No reliable reproduction — keep retrying until the test node can
  replay the failure deterministically.
- Hypothesis chosen before evidence — anti-pattern; return to phase 5.
- Patch fixes symptom but not cause — next test run will resurface
  the bug.
- Missing regression test — future iterations will repeat the fix.

## Recovery moves

- If evidence stays noisy, build a tighter reproduction harness.
- If speculative changes pile up, revert and return to a known
  failing baseline.
- Reduce the diff until each change has a reason.

## Anti-patterns

- Editing first, explaining later.
- Assuming the top stack frame is the root cause.
- Mixing refactor work into a debug patch.
- Adding logs everywhere.
- Declaring victory after one pass on a flaky issue.

## Checklist

- [ ] Stabilized the report (symptom written down)
- [ ] Reproduced the failure
- [ ] Narrowed the surface area
- [ ] Instrumented selectively (minimal)
- [ ] Ranked hypotheses (≥3)
- [ ] Proved or killed them
- [ ] Patched the true cause
- [ ] Verified with the reproducer
- [ ] Added regression protection
- [ ] Saved a durable lesson if one exists
