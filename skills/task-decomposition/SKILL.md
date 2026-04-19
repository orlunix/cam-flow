---
name: task-decomposition
description: Break a complex cam-flow node into independent sidecar subtasks with disjoint file ownership, one owner per slice, and explicit integration points. Use when a single fix node would touch too many files, mix concerns (implementation + verification + docs), or when the critical path is local but sidecar work (codebase mapping, docs verification, regression tests) can advance in parallel. In the CAM phase today, "subagent" = a separate camc-spawned agent triggered by a downstream node; this skill also works as a decomposition checklist for serial execution when parallelism is not yet wired.
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  ported_from: hermes-CCC subagent-driven-development (NousResearch Hermes Agent)
  adapted_for: cam-flow CAM-phase complex fix nodes
  category: orchestration
  tags:
    - decomposition
    - parallelism
    - subagents
    - coordination
  maturity: beta
---

# Task Decomposition (cam-flow adaptation)

## Purpose

- Shorten total workflow time by running independent streams in
  parallel (when the engine supports parallel nodes) or serially
  (when it does not — the decomposition checklist still helps).
- Keep the critical path moving locally; delegate sidecar work.
- Prevent one giant fix node from thrashing across files.
- Assign explicit ownership to each slice so concurrent edits don't
  race.

## When to activate

- `task-router` classified the workflow as `deep` or
  `parallelism: decomposable`.
- The node's `with` field mixes implementation, verification, and
  research concerns.
- The CONTEXT block shows `active_state.key_files` spanning ≥ 3
  distinct subsystems.
- A single agent prompt would exceed the comfortable context budget.

## When NOT to activate

- The next local action is blocked on the answer — do it yourself.
- The task is small enough to finish faster alone.
- The subtasks touch the same files and would conflict.
- The problem is still poorly framed — route and frame first.

## Core principle

- Blocking work: local. Sidecar work: delegated.
- Explicit ownership per slice.
- Integrate results quickly — don't let sidecars diverge.
- Do not re-do delegated work yourself.

## Procedure

1. **Identify the deliverable.** What does "done" look like for this
   node's task?
2. **Identify the next blocking action.** The one step whose
   result gates everything else. Keep this local.
3. **List parallel sidecar work.** Mapping, verification, docs
   checks, targeted regression tests in disjoint files.
4. **Split by independent outputs or disjoint write sets.** One
   owner per file family.
5. **Write one concrete prompt per subagent.** State the task in
   one sentence; name the owned files/modules; note the subagent is
   not alone in the repo; state what to return.
6. **Plan integration.** Where do the streams re-converge? Usually
   the next `test` or `verify` node.
7. **Emit the decomposition block** to `state_updates.next_steps`
   and `state_updates.active_task`.

## Good subtasks

- Codebase mapping for a specific subsystem.
- Docs-backed verification (read-only).
- Isolated backend patch in owned files.
- Isolated frontend patch in owned files.
- Targeted regression test creation in a separate file family.
- Review of a completed patch.

## Bad subtasks

- "Figure out the whole problem."
- "Do whatever seems useful."
- Work that overlaps the same file region as the critical path.
- Urgent work needed for the next local decision.

## Prompt design rules for subagent nodes

Each subagent node's `with` field should:

- State the task in one sentence.
- Name the owned files or module boundary.
- Note that the subagent is not alone in the codebase ("do not revert
  unrelated edits").
- Define the expected final output.
- Include any known constraints or acceptance criteria.

### Example prompt

```yaml
map_parser:
  do: subagent claude
  with: |
    Map the code path from parser/tokenizer.py through
    parser/ast_builder.py. Owned files: parser/*. You are not alone
    in the codebase; do not modify non-parser files. Return: a
    numbered list of call sites and any non-obvious invariants.
  next: integrate
```

## Ownership rules

- One write owner per file family whenever possible.
- Shared read-only context is fine (multiple nodes may grep the
  same file).
- Shared write scope is a last resort — and if used, one node must
  be the integrator.
- Verification nodes stay read-only unless asked to patch tests.

## Integration procedure

1. Review each sidecar node's output from the trace.
2. Check whether each stayed within scope (compare
   `state_updates.files_touched` against the owned file set).
3. Integrate into the critical path locally.
4. Reconcile any assumptions that conflict with local work.
5. Run the validation node.

## Conflict management

- If two streams unexpectedly touch the same file, stop and replan —
  do not merge blindly.
- Preserve the clearer ownership model after replanning.
- Prefer one integrator making final decisions.

## Fallback when parallel execution is unavailable

The CAM engine in v0.1 is single-threaded; parallel nodes are a
Month 3+ feature (roadmap §8 #10). Until then:

- Use the same decomposition framework serially.
- Convert each sidecar slice into a sequential node in the workflow.
- Preserve explicit ownership in the node's `with` field even though
  only one node runs at a time.

## Output contract (cam-flow)

When this skill runs inside a planning node, write
`.camflow/node-result.json` with:

```json
{
  "status": "success",
  "summary": "Decomposed <task> into <N> slices",
  "state_updates": {
    "active_task": "<critical path>",
    "next_steps": [
      "critical: <local task description>",
      "sidecar: <delegated task>, owner <files>",
      "verify: <validation step>"
    ],
    "decomposition": [
      {"slice": "map_parser", "owner": "parser/*", "type": "sidecar"},
      {"slice": "patch_tokenizer", "owner": "parser/tokenizer.py", "type": "critical"},
      {"slice": "regression_test", "owner": "tests/parser/*", "type": "sidecar"}
    ]
  },
  "error": null
}
```

## Decision rules

- Delegate only if the subtask is concrete and bounded.
- Keep ambiguity resolution local.
- Parallelize information gathering aggressively when it is
  independent.
- Avoid delegating more slices than integration can absorb.
- If the main path is short, skip delegation.

## Failure modes

- Delegation of blocking work.
- Overlapping file ownership causing merge conflicts.
- Weak prompts that encourage broad wandering.
- Premature waiting on sidecars.
- Local duplication of delegated work.

## Recovery moves

- Cancel or redirect a drifting sidecar quickly.
- Re-scope overlapping workers into disjoint ownership.
- Absorb a small sidecar locally if integration overhead dominates.
- Move verification later if implementation is still unstable.

## Checklist

- [ ] Identified the deliverable
- [ ] Identified the blocking task (kept local)
- [ ] Listed parallel sidecar tasks
- [ ] Split by disjoint file ownership
- [ ] Wrote precise prompts for each slice
- [ ] Defined integration point
- [ ] Defined validation plan
