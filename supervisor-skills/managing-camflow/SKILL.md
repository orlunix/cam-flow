---
name: managing-camflow
description: Design, tune, launch, monitor, recover, and finish Camflow v1.2 workflows as a supervisor. Use when an agent must turn `camflow plan` output into a reviewed static graph, choose useful node boundaries and routes, inject domain memory such as agent-debug-wiki, run Camflow through camc, inspect durable run state, decide between resume, run-from, or a new workflow revision, and supervise the flow until a verified terminal result.
---

# Managing Camflow

Act as the owner of the whole Camflow lifecycle. Prepare the graph and its
inputs, start the runner, observe durable state, intervene only at explicit
halts, and keep ownership until the run is verified as done or a human decision
is required.

Keep this boundary:

```text
supervisor = design + memory policy + launch + observe + recovery decision
Camflow    = validate + schedule + route + retry + verify + persist
camc       = create + archive + stop + remove agent sessions
```

Do not manually schedule Camflow nodes or replace its retry, verifier, routing,
archive, or cleanup behavior.

## Use the current contract

Locate `dist/camflow` or the installed `camflow` and confirm `camflow version`.
Read the repository's current `README.md` and `docs/design-v1.2.md` before
changing a graph. Treat v1.1 files and `src/runner/` as historical.

Use only the v1.2 static graph model:

- Treat `input.json` as immutable run-wide input.
- Pass node results only through declared `needs` edges.
- Route only through restricted `when` conditions.
- Use local `skills/<name>/SKILL.md`; do not depend on an implicit host fallback.
- Keep graph topology fixed during a run.
- Use files in the run directory as source of truth, not chat history.

Read [graph-design.md](references/graph-design.md) before creating or tuning a
workflow. Read [operations.md](references/operations.md) before launching,
monitoring, or recovering a run.

## Supervise the lifecycle

### 1. Establish the objective and memory policy

Write one concrete workflow goal and completion condition. Identify the real
case inputs, available deterministic checks, relevant project constraints, and
the memory provider that applies to the domain.

For RTL debug, prefer `$agent-debug-wiki` when it is available. Verify that the
worker environment can access it before claiming memory integration. Require
every agent node to consult relevant memory, report `memory_refs`, and validate
old knowledge against current evidence. Never treat memory as ground truth.

Decide whether the run should only propose a memory update or may write it.
Require explicit authorization and an idempotency key for external memory
writes.

### 2. Generate a template, then redesign it

Use `plan` only to materialize an editable starting directory:

```bash
./dist/camflow plan "debug case_id=bug_001 sim_log=/runs/bug.log trace_log=/runs/trace.log" --out .camflow/plan/rvdbg
```

Never run the generated workflow unchanged. The current planner emits a broad
single-node draft; it does not know the required engineering decomposition,
route vocabulary, verification gates, memory policy, or completion criteria.

Tune `workflow.yaml`, `input.json`, `input.template.json`, local node skills,
and validators. Review the result against the checklist in
[graph-design.md](references/graph-design.md). Keep the authoring directory
separate from cases and run directories.

### 3. Validate before launch

Confirm all of the following:

- The top-level workflow name is short and meaningful.
- Every node has one coherent outcome and a useful durable handoff.
- Every `needs` edge carries data or sequencing that the consumer requires.
- Every branch value is finite, explicit, and covered exactly once.
- Every declared output field is actually needed downstream or for audit.
- Deterministic verification is used when available; agent criteria are used
  only for semantic judgments.
- Retry counts are bounded; use `retry: 1` as the normal recovery budget.
- Every agent node and semantic verifier has an explicit memory-read
  instruction; node outputs record `memory_refs`.
- A final goal audit exists before optional memory writeback.
- Real paths and values are in `input.json`; placeholders exist only in
  `input.template.json`.

Optionally use `camflow pack` after tuning. Then inspect the packed bundle and
ensure every runtime-required local skill is present, including `evaluator`
when a node uses criterion/default agent verification. Do not assume `pack`
repairs an incomplete graph or skill set. If `evaluator` is absent, copy the
checked-in local evaluator skill into the bundle before running it.

### 4. Trigger one owned run

Choose a new empty run directory and run the reviewed workflow in the
foreground or another durable session owned by the supervisor:

```bash
./dist/camflow run packages/rvdbg/workflow.yaml --input cases/bug_001.json --run-dir runs/rvdbg/bug_001-a
```

Do not launch individual node agents. Camflow creates them through camc with
names and tags derived from the workflow and run identity.

Treat the process exit and persisted files together:

- Exit `0` plus `workflow_completed` means `done`.
- Exit `2` plus `halt.json` means `halted` and needs a recovery decision.
- Exit `1` means invocation, schema, snapshot, or other validation error.

### 5. Monitor without taking over scheduling

Keep observing until a terminal state. Follow `trace.jsonl`, inspect the active
node attempt, and use the exact flow tag from `run.json` to find live camc
workers. Avoid tight polling and do not call a node stuck merely because a
long agent has not yet produced output.

Escalate investigation when the configured agent timeout is approaching, the
worker disappears without an output, the same failure repeats, or Camflow
exits. Preserve all evidence before corrective action.

### 6. Recover deliberately

Read `halt.json`, the halted attempt's `output.json`, `verify.json`,
`camc-lifecycle.json`, and the tail of `trace.jsonl`. Classify the cause before
issuing a recovery command.

Use `resume` when the graph and immutable input are still correct and the
halted node only needs concrete feedback, missing information, or recovery
from a transient executor problem:

```bash
./dist/camflow resume runs/rvdbg/bug_001-a --feedback "Use the timeout window in trace.log and cite exact cycles."
```

Use `run --from` only when a previously completed node and all of its
downstream results must be recomputed. Preserve a checkpoint of the run first,
because run-from removes the selected node's persisted directory and all
downstream node directories:

```bash
./dist/camflow run --from test_or_dut --run-dir runs/rvdbg/bug_001-a --feedback "Emit exactly lsu_debug or ifu_debug."
```

Do not edit the snapshotted workflow or input in a run directory. If node
boundaries, edges, skills, schemas, or allowed routes are wrong, tune the
authoring copy, assign a new revision/run directory, and start a fresh run.

Never loop resumes indefinitely. After one repeated identical failure, revisit
the node contract or ask for human direction; after two supervisor-driven
recovery attempts, stop unless new evidence materially changes the diagnosis.

### 7. Close the run

Declare completion only when the trace contains `workflow_completed`, required
outputs and verification evidence exist, no unresolved `halt.json` remains,
and the final audit demonstrates the original workflow goal.

Report the workflow name, flow ID, run directory, selected route, final
artifacts, verification evidence, memory references, memory update result or
proposal, and any remaining risk. If the run cannot finish without a decision,
leave the artifacts intact and ask the human the smallest concrete question.
