---
name: task-router
description: Classify a cam-flow workflow's opening task and pick the execution approach. Use this skill in `start` or `analyze` nodes to label the task class (lightweight / standard / deep), choose the reasoning depth, decide what to read first (CLAUDE.md / repo docs / failing output), and whether the workflow should decompose into parallel sidecar streams. The engine's methodology_router independently injects methodology hints by keyword; task-router is the full triage pass for the workflow's first decision.
version: 1.0.0
author: cam-flow
license: MIT
metadata:
  ported_from: hermes-CCC hermes-route (NousResearch Hermes Agent)
  adapted_for: cam-flow CAM-phase start/analyze nodes
  category: orchestration
  tags:
    - routing
    - planning
    - triage
    - start-node
  maturity: beta
---

# Task Router (cam-flow adaptation)

## Purpose

- Route the workflow before execution instead of discovering
  complexity mid-loop.
- Decide whether the task is **lightweight**, **standard**, or
  **deep-investigation** work.
- Decide what to read first (CLAUDE.md, failing test output, relevant
  code, external refs).
- Decide whether the workflow should decompose into parallel
  subagent streams (see `task-decomposition` skill).
- Emit a compact routing block to `state_updates.active_task` and
  `state_updates.next_steps` so downstream nodes start with the
  right framing.

## When to activate

- This is a `start` or `analyze` node (usually the first real work
  node after any initial cmd check).
- The CONTEXT block is empty or minimal (first iteration).
- The workflow's goal is ambiguous, mixes concerns, or spans multiple
  files.
- The request includes pasted code, logs, diffs, URLs, or multiple
  numbered objectives.

## Inputs to assess

- The node's `with` field (user-provided task description)
- Any prior test output in `state.test_output`
- `state.active_task` (empty on first run)
- Repository signals: presence of `CLAUDE.md`, `README.md`, failing
  tests, stack traces, commit history in `git log`

## Complexity buckets

### Lightweight
Single factual question, small formatting or wording change,
one-file trivial edit, simple transformation with no hidden state.
→ Skip decomposition. Proceed directly to a fix-or-edit node.

### Standard
One feature or bug across a small number of files, straightforward
repo navigation, routine API/CLI integration, clear user goal.
→ Read the target files, then proceed to the existing fix/test loop.

### Deep
Bug with unclear root cause; refactor with behavioral risk;
architecture question; code review over a large diff; request that
mixes implementation, validation, and migration.
→ Load the `systematic-debugging` methodology; consider invoking
`task-decomposition` if sidecar streams exist.

## Procedure

1. **Identify the deliverable.** What is the workflow expected to
   produce when it reaches `done`? Write this to
   `state_updates.active_task` as a one-line goal.
2. **Identify mindset.** Is the user asking for implementation,
   review, research, or planning?
3. **Scan for hard signals.** Code blocks, logs, diffs, URLs,
   numbered asks, references to bugs or production issues.
4. **Estimate the cost of the wrong first step.** If the wrong first
   step is expensive (large diff, auth changes, payments), bump the
   class toward `deep`.
5. **Check repo-level constraints.** Is there a CLAUDE.md or
   AGENTS.md? Set `state_updates.next_steps` to include reading them.
6. **Decide what to read first.** Memory / docs / failing output /
   relevant module.
7. **Decide whether the work is serial or parallelizable.** If
   parallelizable, include `invoke task-decomposition` in
   `next_steps`.
8. **Emit the routing block.** Include task class, execution mode,
   read-first list, parallelism, reasoning depth, why, first
   concrete step.

## Decision rules

- Default to `standard` when the user's goal is clear and scope is
  bounded. Upgrade to `deep` if any hard signal appears.
- Favor reading CLAUDE.md / AGENTS.md first when a repo has one.
- Favor reading the failing test output first when
  `state.test_output` is non-empty.
- Never use `lightweight` for debugging ambiguous failures,
  refactors, or reviews of risky diffs.
- If the request mixes "explain + implement + review", route each
  concern separately — probably a decomposition case.

## Output contract (cam-flow)

Write `.camflow/node-result.json` with:

```json
{
  "status": "success",
  "summary": "Routed task as <class>, first step: <summary>",
  "state_updates": {
    "active_task": "<one-line deliverable>",
    "task_class": "lightweight|standard|deep",
    "reasoning_depth": "low|medium|high",
    "parallelism": "none|decomposable",
    "next_steps": [
      "Read CLAUDE.md",
      "Read failing test output",
      "<first concrete step>"
    ]
  },
  "error": null
}
```

Emit it as a compact routing block in the summary so trace.log
rollups can aggregate task classes across runs.

## Example output

```markdown
Task class: deep
Execution mode: implement with plan
Read first: CLAUDE.md, failing pytest output, calculator.py
Parallelism: no
Reasoning depth: high
Why: bug fix with multi-function scope (divide + factorial + power);
     prior test_output shows 5 failures across 3 subsystems.
First concrete step: reproduce the failure and trace the divide()
                     zero-check path.
```

## Failure modes

- Overrouting a trivial task into unnecessary planning (makes the
  workflow slower without improving quality).
- Underrouting a risky task and making premature edits.
- Ignoring repo instructions that change the allowed workflow.
- Delegating work before the problem is framed.

## Recovery moves

- If new ambiguity appears in a later node, re-run routing from that
  node's fix attempt rather than forcing execution.
- If the first read reveals larger scope, upgrade from `standard` to
  `deep` via a subsequent fix-retry with an escalation banner.

## Checklist

- [ ] Identified the deliverable
- [ ] Identified mindset (implement / review / research / plan)
- [ ] Checked for code, logs, URLs, multi-part asks
- [ ] Decided complexity bucket
- [ ] Decided what to read first
- [ ] Decided whether to decompose
- [ ] Wrote the routing block to `node-result.json`
