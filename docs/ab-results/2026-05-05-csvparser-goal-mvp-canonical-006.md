# A/B Result - value-demo Canonical Run 006 - 2026-05-05

> Goal-driven MVP validation run. Real `camflow run` from a fresh
> value-demo fixture completed first-pass with a 5-node workflow,
> top-level `goal:`, `dag_revisions/0001/`, and per-requirement final
> audit evidence. Score: **97/100**.

## Run Metadata

- camflow HEAD: `50638b1` (`fix(goal-driven): inject Workflow.goal into run + verify prompts`)
- Fresh fixture: `/tmp/camflow-canonical-20260505-goal-mvp/camflow`
- Run dir: `/tmp/camflow-canonical-20260505-goal-mvp/camflow/.camflow/run`
- Command:

```bash
cd /tmp/camflow-canonical-20260505-goal-mvp/camflow
camflow run "$(cat /home/hren/.openclaw/workspace/camflow/examples/value-demo/PROMPT.txt)"
```

Result: `done`.

## Generated DAG

Planner emitted one active user execution DAG revision:

```text
analyzer
  -> implementer
      -> default_test_audit
      -> invariant_audit
          -> reviewer
```

The compiled workflow includes top-level `goal:`:

> Implement parse_record(line: str) -> list[str] in lib/csvparser.py so all four SPEC.md requirements are satisfied, every test in tests/ and tests/invariants/ passes, and no other code or tests in the project are modified or broken.

`dag_revisions/0001/` was recorded before user-node execution:

- `.camflow/run/dag_revisions/0001/workflow.yaml`
- `.camflow/run/dag_revisions/0001/manifest.json`

Manifest fields:

```json
{
  "revision": 1,
  "parent_revision": null,
  "reason": "initial_plan",
  "workflow_goal": "Implement parse_record(line: str) -> list[str] in lib/csvparser.py so all four SPEC.md requirements are satisfied, every test in tests/ and tests/invariants/ passes, and no other code or tests in the project are modified or broken.\n"
}
```

All top-level trace events include `dag_revision: 1`.

## Score

`python examples/value-demo/scripts/score.py /tmp/camflow-canonical-20260505-goal-mvp/camflow`

| category | points | evidence |
|---|---:|---|
| requirement coverage | 35/35 | visible + invariant tests pass |
| test correctness | 20/20 | `bash scripts/run_all_tests.sh` -> 4 passed |
| evidence quality | 15/15 | reviewer produced per-requirement evidence with `lib/csvparser.py:line` citations and passing test names |
| process auditability | 15/15 | 22 trace events, 5 node attempts, audit nodes present |
| robustness/minimality | 7/10 | `diff_lines=43` |
| resilience | 5/5 | first-pass done, bounded retry configured, 0 retry churn |
| **TOTAL** | **97/100** | |

## Evidence Notes

Reviewer approved with concrete evidence for all four requirements:

- Req 1: comma splitting, `lib/csvparser.py:14-47`, plus visible test.
- Req 2: surrounding whitespace stripping, `lib/csvparser.py:41`, plus invariant test.
- Req 3: quoted field with inner commas, `lib/csvparser.py:19-36`, plus invariant test.
- Req 4: doubled quote escape, `lib/csvparser.py:24-27`, plus invariant test.

Scope check passed: only `lib/csvparser.py` changed in the fixture.

## Regression Asset

This run confirms `examples/value-demo/` remains a useful scored
regression benchmark after the goal-driven MVP changes. Future
Planner/runtime work should keep this harness and compare against this
run or later canonical runs rather than replacing the fixture.
