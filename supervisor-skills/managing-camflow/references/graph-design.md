# Camflow graph design

Use this guide to turn a planner draft into a small, stable v1.2 graph.

## Prefer semantic checkpoints

Define a node as one coherent unit of work that produces an independently
useful, inspectable result. With stronger agents, prefer fewer capable nodes
over tool-call-sized nodes. Do not confuse a workflow step with a node.

Keep work in one node when all of these hold:

- It has one primary objective and one output contract.
- It uses the same domain context and worker skill throughout.
- One verifier can judge the whole result.
- A capable agent can complete it in one bounded session.
- Retrying the whole unit is safe and reasonably cheap.

Split a node when any of these hold:

- A typed decision controls different downstream expertise.
- A deterministic fact-gathering stage should precede semantic analysis.
- Separate workers need distinct tools, memory, permissions, or context.
- An intermediate artifact is worth preserving or reusing on recovery.
- Independent verification should reject one stage without rerunning another.
- The work has a high-risk or externally visible side effect.
- The prompt contains multiple outcomes that can succeed or fail independently.

Merge adjacent nodes when the first only paraphrases or forwards data, has no
independent verification value, and would never be resumed separately.

As a starting heuristic, aim for 3-6 semantic agent nodes for an ordinary
engineering flow. Treat this as a smell detector, not a limit. A one-node flow
is valid for one independently verifiable task; ten nodes are valid only when
each boundary earns its scheduling, audit, or recovery cost.

## Tune planner output

Treat `camflow plan` output as scaffolding, not a reviewed design. Perform this
sequence before running it:

1. Replace `generated_debug_plan` with a short domain name such as `rvdbg`.
2. Rewrite `goal` as a measurable end state rather than an activity.
3. Put stable policies in `context`: evidence rules, memory provider, safety
   boundaries, and current-case-over-memory precedence.
4. Keep `input_schema` to immutable facts shared by the whole run. Move no
   intermediate agent result into `input.json`.
5. Identify decision points, durable artifacts, deterministic gates, and
   external side effects.
6. Draw the smallest DAG that exposes those boundaries.
7. Give each node a verb-led ID, one goal, concrete steps, the minimum
   `output_schema`, and a local skill matched to its work.
8. Add `needs` only for direct data/sequencing dependencies. Remember that a
   node sees successful output only from its direct dependencies.
9. Add a deterministic `verify.command` where a program can decide. Use a
   focused `verify.criterion` for semantic evidence and completeness.
10. Add a final goal audit. Add memory reflection/writeback only after it.

Reject a design if it relies on hidden shared mutable state, dynamic nodes,
implicit host skills, unbounded retry, or an agent interpreting arbitrary
next-node names.

## Design routing explicitly

Use an ordinary node to make a typed decision, then use `when` as the router.
The decision field must be declared as `string`; each branch must list the
decision node in `needs`; each `(node, path, equals)` value must be unique.

For the `test_or_dut` example, use a finite vocabulary:

```yaml
- id: test_or_dut
  goal: Classify the failure into exactly one supported debug domain.
  steps:
    - Consult the applicable debug memory and record memory_refs.
    - Compare current log and trace evidence; current evidence overrides memory.
    - Emit route as exactly lsu_debug or ifu_debug, with cited evidence.
  run:
    skill: analyzer
  output_schema:
    route: string
    evidence: array
    memory_refs: array
  verify:
    criterion: Accept only an evidence-backed route equal to lsu_debug or ifu_debug.
  retry: 1

- id: lsu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: lsu_debug}
  goal: Produce an evidence-backed LSU root-cause analysis.
  steps:
    - Consult relevant LSU entries in agent-debug-wiki and record memory_refs.
    - Inspect current-case evidence and test remembered hypotheses against it.
    - Emit ranked hypotheses, evidence, and next checks.
  run:
    skill: analyzer
  output_schema:
    hypotheses: array
    evidence: array
    memory_refs: array
  retry: 1

- id: ifu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: ifu_debug}
  goal: Produce an evidence-backed IFU root-cause analysis.
  steps:
    - Consult relevant IFU entries in agent-debug-wiki and record memory_refs.
    - Inspect current-case evidence and test remembered hypotheses against it.
    - Emit ranked hypotheses, evidence, and next checks.
  run:
    skill: analyzer
  output_schema:
    hypotheses: array
    evidence: array
    memory_refs: array
  retry: 1
```

Let a downstream report/audit node depend on both branch nodes. A skipped
branch counts as complete, but Camflow injects only successful branch output
into `upstream`.

Do not add a catch-all branch unless it has real work and semantics. If the
classifier emits an unsupported value, let Camflow halt with
`unmatched_route`; then correct the classifier via run-from or revise the graph
if the vocabulary was incomplete.

## Make memory explicit

Separate four concerns:

1. **Availability:** Confirm the worker can invoke/read the selected memory
   provider, such as `$agent-debug-wiki`.
2. **Read contract:** Add a memory-read step to every agent node and require a
   `memory_refs: array` field. Permit an explicit `unavailable` marker only
   when memory is optional.
3. **Evidence policy:** Treat memory as hypotheses and prior art. Require every
   conclusion to cite current run evidence; record conflicts instead of
   silently choosing memory.
4. **Writeback:** Record only reusable, evidence-backed, non-secret lessons.

Do not turn memory into mutable Camflow global data. Reads are explicit worker
actions; information that must affect downstream execution belongs in the
node's declared output and a `needs` edge.

Apply the same read capability to semantic verifier agents. Put the relevant
memory instruction in `verify.criterion` because verifier prompts do not
inherit the workflow context or node steps. Ask the evaluator to record the
references it used in `data.memory_refs` or its reasoning; the raw verifier
envelope remains in the attempt's `verify/` directory. Deterministic
`verify.command` checks are programs, not agents, and need no memory lookup.

Prefer adding a reflection step to the final report node when it only decides:

```text
memory_update_needed
memory_delta
memory_refs
```

Add a dedicated final `update_memory` node only when it performs an actual
write, requires a different skill/permission, deserves an independent audit,
or must be retried separately. Make the write depend on the final goal audit,
use an idempotency key such as `<workflow>:<case_id>:<finding-signature>`, and
return the exact entry identifiers changed. On replay or resume, check for the
key before writing again.

When writes are not pre-authorized, emit a proposed memory delta and let the
supervisor request approval rather than mutating the wiki.

## Check the final graph

Before launch, answer yes to each question:

- Can every node be described by one sentence and verified by one gate?
- Would deleting any edge make its consumer miss required data or ordering?
- Are route values explicit and fully represented by branch nodes?
- Does each agent node and semantic verifier know how to consult memory and
  report what it used?
- Can each failed node be retried safely without repeating an uncontrolled
  side effect?
- Does the final audit cover the workflow goal rather than only node status?
- Does memory writeback happen only after the result is accepted?
