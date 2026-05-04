# A/B Result - Csvparser Value Demo - 2026-05-04 Run 001

> ⚠ **Provisional / non-canonical.** The CamFlow leg here was launched
> via `python -m runner.runtime`, not via the user-facing `camflow run`
> CLI required by `examples/value-demo/AB-PROTOCOL.md`. Results are
> kept for reference only; do not treat the 85/100 score as a
> canonical CamFlow A/B number. The next valid A/B must use
> `camflow run "$(cat examples/value-demo/PROMPT.txt)"` from inside a
> fresh fixture copy.

## Run Metadata

- Prompt: `examples/value-demo/PROMPT.txt`
- Prompt text: `Implement parse_record in lib/csvparser.py per SPEC.md. Make all tests pass. Don't break anything else.`
- Baseline fixture: `/tmp/camflow-ab-20260504-010134/baseline`
- CamFlow fixture: `/tmp/camflow-ab-20260504-010134/camflow`
- Baseline agent: `fec525f5` (`value-demo-baseline-010134`)
- CamFlow command: `PYTHONPATH=/home/hren/.openclaw/workspace/camflow/src python -m runner.runtime run "$(cat examples/value-demo/PROMPT.txt)"`
- CamFlow result: `done`

## Summary

Both paths solved the implementation task: visible test passed, all three
invariant tests passed, and both produced a focused `lib/csvparser.py`
implementation.

The meaningful difference in this run is structural, not final code
correctness. The single agent did well and read the spec/tests directly.
CamFlow produced durable per-node artifacts and a planner/execution trace,
but the Planner generated a smaller 3-node DAG rather than the intended
5-node reference DAG with explicit `test_runner` and `invariant_checker`
tool audit nodes. No retry occurred because the first CamFlow implementation
attempt passed all tests.

## Score Table

Manual scoring used the same 100-point rubric in
`docs/e2e-ab-score-protocol-codex-2026-05-04.md`.

| Category | Weight | Single camc | CamFlow | Evidence |
|---|---:|---:|---:|---|
| Requirement coverage | 35 | 35 | 35 | both score JSON files: visible=1, invariants=3 |
| Test correctness | 20 | 20 | 20 | both `pytest` checks pass |
| Evidence quality | 15 | 8 | 13 | baseline transcript vs CamFlow node envelopes |
| Process auditability | 15 | 1 | 10 | baseline transcript only vs `.camflow/run` artifacts |
| Robustness/minimality | 10 | 7 | 7 | both `diff_lines=37` |
| Recovery behavior | 5 | 0 | 0 | no retry/self-correction happened in this run |
| Total | 100 | 71 | 85 | CamFlow +14 |

## Evidence

Baseline:

- Transcript: `/tmp/camflow-ab-20260504-010134/baseline.transcript`
- Score: `/tmp/camflow-ab-20260504-010134/baseline.score.json`
- Diff: `/tmp/camflow-ab-20260504-010134/baseline.diff`
- Result: visible tests pass; invariant tests pass.

CamFlow:

- Workflow: `/tmp/camflow-ab-20260504-010134/camflow/.camflow/run/workflow.yaml`
- Trace: `/tmp/camflow-ab-20260504-010134/camflow/.camflow/run/trace.jsonl`
- Node outputs:
  - `nodes/gather_context/attempt-1/agent_output.json`
  - `nodes/implement/attempt-1/agent_output.json`
  - `nodes/regression_review/attempt-1/agent_output.json`
- Score: `/tmp/camflow-ab-20260504-010134/camflow.score.json`
- Diff: `/tmp/camflow-ab-20260504-010134/camflow.diff`
- Result: visible tests pass; invariant tests pass.

## Diagnostic Findings

- The case was fair enough that the single agent solved it in one pass.
  This is not a failure of the project; it means this particular run
  differentiates mostly on auditability, evidence, and workflow artifacts.
- CamFlow also solved it in one pass and produced useful artifacts:
  Planner run, compiled workflow, trace, per-node prompts, agent outputs,
  and runtime-validated envelopes.
- Planner did not generate the intended 5-node DAG from
  `examples/value-demo/workflow-reference.yaml`. Actual nodes were:
  `gather_context`, `implement`, `regression_review`.
- Because Planner did not include explicit `test_runner` /
  `invariant_checker` tool nodes, this run did not demonstrate the full
  intended tool-gated audit structure.
- Because implementation passed first try, neither side demonstrated
  recovery behavior. Recovery remains tested deterministically by
  `tests/test_e2e_value.py`, but not demonstrated in this live LLM run.

## Next Review Target

The harness is useful: it produced a real comparison and surfaced a
specific CamFlow improvement area. The next development target should be
Planner behavior: for this prompt/fixture, Planner should emit a DAG closer
to `examples/value-demo/workflow-reference.yaml`, with explicit tool audit
nodes and the implementer verification gate that can trigger retry.

