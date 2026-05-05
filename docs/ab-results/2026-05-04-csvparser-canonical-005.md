# A/B Result — value-demo Canonical Run 005 — 2026-05-04

> ✅ **First-pass success with audit nodes.** Planner generated the
> full 5-node shape (analyzer / implementer / test_runner_default /
> test_runner_invariants / reviewer); every node completed first-pass;
> reviewer cited per-requirement evidence including audit-envelope
> data; all 4 tests pass. **Score: 97/100.**

## Run metadata

- camflow repo: `/home/hren/.openclaw/workspace/camflow` @ HEAD before
  this commit. Includes the new audit-node-mandatory + per-script
  schema-match prompt updates.
- cam: `/home/hren/.openclaw/workspace/cam` @ tip = local working
  state with the three layered fixes (trust dialog, TOML parser,
  tmux send-keys chunking) — chunking patch still uncommitted on
  cam-dev's tree.
- Deployed `camc`: `/data/venv/bin/camc` (editable install picks up
  source changes immediately). v1.2.0 (dev).
- Claude Code: v2.1.126.
- Fresh fixture: `/tmp/camflow-canonical-20260504-184701/camflow`.
- Run dir:
  `/tmp/camflow-canonical-20260504-184701/camflow/.camflow/run/`.

## Command

```bash
cd /home/hren/.openclaw/workspace/camflow
PFX=/tmp/camflow-canonical-20260504-184701
mkdir -p "$PFX"
bash examples/value-demo/scripts/setup-fixture.sh "$PFX/camflow"
( cd "$PFX/camflow" && \
    camflow run "$(cat /home/hren/.openclaw/workspace/camflow/examples/value-demo/PROMPT.txt)" )
```

`camflow` printed (excerpt):
```
compiling prompt via Planner → /tmp/.../planner
executing compiled workflow → /tmp/.../run
result: done
```

Wall-clock: ~11 minutes (Planner 7min + user workflow 4min).

## Generated DAG (Planner output)

The compiled `workflow.yaml` has **5 user nodes**:

| id                       | run                            | needs                                                        | retry | verify                                            | output_schema                                                |
|--------------------------|--------------------------------|--------------------------------------------------------------|-------|---------------------------------------------------|--------------------------------------------------------------|
| analyzer                 | skill: analyzer                | —                                                            | 2     | (default agent verify)                            | requirements: array, test_paths_referenced: array            |
| implementer              | skill: code_writer             | [analyzer]                                                   | 3     | command: walk-up SPEC.md → bash run_all_tests.sh  | files_changed: array, summary: string                        |
| test_runner_default      | tool: scripts/run_default_tests.sh | [implementer]                                            | 1     | command: jq .data.passed = true                   | passed: boolean, tests_run: integer, output: string          |
| test_runner_invariants   | tool: scripts/run_invariants.sh    | [implementer]                                            | 1     | command: jq .data.passed = true                   | passed: boolean, tests_run: integer, failed_tests: array, output: string |
| reviewer                 | skill: reviewer                | [analyzer, implementer, test_runner_default, test_runner_invariants] | 2 | (default agent verify)                            | approved: boolean, issues: array                             |

**This is the structural shape canonical-002 was missing.** Both
audit tool nodes were generated AND with **per-script-correct
output_schema**:

- `test_runner_default` — `passed/tests_run/output` (matches what
  `scripts/run_default_tests.sh` actually emits; no `failed_tests`).
- `test_runner_invariants` — `passed/tests_run/failed_tests/output`
  (matches `scripts/run_invariants.sh`).

The schema mismatch that halted canonical-003 (where Planner had
declared a uniform 4-field schema for both audit nodes) is resolved
by the prompt-side fix only — fixture scripts are unchanged.

## Trace summary (top-level user workflow, 22 events)

```
analyzer attempt-1                  success (15:54:01 → 15:55:19)
implementer attempt-1               success (15:55:19 → 15:56:43)
test_runner_default attempt-1       success (15:56:43 → 15:56:43, <1s)
test_runner_invariants attempt-1    success (15:56:43 → 15:56:43, <1s)
reviewer attempt-1                  success (15:57:19 → 15:58:23)
workflow_completed                  status=success
```

`retry_triggered` events: **0**. First-pass success across all 5
nodes. Bounded retry configured on every skill node (analyzer 2,
implementer 3, reviewer 2).

## Tests on the produced implementation

```
$ cd /tmp/camflow-canonical-20260504-184701/camflow
$ bash scripts/run_all_tests.sh
....                                                                     [100%]
4 passed in 0.01s
```

4 tests total: 1 visible (`tests/test_csvparser.py::test_basic_split`)
+ 3 invariant
(`tests/invariants/test_invariants.py::test_strip_surrounding_whitespace`,
`::test_quoted_field_with_comma`,
`::test_doubled_quote_inside_quoted_field`). All pass.

Diff vs pristine: ~50 lines (lib/csvparser.py only). Scope: only
`lib/csvparser.py` was modified; signature `parse_record(line: str)
-> list[str]` preserved.

## Score table

| category                | weight | score | source / rationale                                                                                       |
|-------------------------|--------|-------|----------------------------------------------------------------------------------------------------------|
| requirement_coverage    | 35     | 35    | auto: 1 visible + 3 invariant tests pass, all 4 SPEC reqs satisfied                                      |
| test_correctness        | 20     | 20    | auto: visible suite + invariants both green                                                              |
| evidence_quality        | 15     | 15    | manual: reviewer cites file:line + passing test name + audit-envelope `data.passed` per req_1..req_4     |
| process_auditability    | 15     | 15    | auto: 5 user nodes, 22 trace events, 5 attempts. **Full marks** thanks to audit tool nodes + envelopes   |
| robustness_minimality   | 10     |  7    | auto: diff_lines=50 → "≤80" bucket; do-not-chase per reviewer                                            |
| resilience              |  5     |  5    | auto: lifecycle done + bounded retry configured on all 3 skill nodes; retry_triggered=0 (zero churn)     |
| **TOTAL**               | **100**| **97**|                                                                                                          |

## Reviewer evidence (excerpt)

```json
{
  "approved": true,
  "issues": [],
  "per_requirement_evidence": {
    "req_1": "lib/csvparser.py:34-43 — outside-quotes comma splits ...; test tests/test_csvparser.py::test_basic_split (test_runner_default envelope: passed=true, 1 test).",
    "req_2": "lib/csvparser.py:36-37 ... preserves inner whitespace at line 56; test ::test_strip_surrounding_whitespace (test_runner_invariants envelope: passed=true).",
    "req_3": "lib/csvparser.py:45-50 ... test ::test_quoted_field_with_comma.",
    "req_4": "lib/csvparser.py:21-25 — adjacent '\"\"' → literal '\"', i+=2; test ::test_doubled_quote_inside_quoted_field."
  },
  "scope_check": "upstream.implementer.data.files_changed == ['lib/csvparser.py']; signature preserved at lib/csvparser.py:7.",
  "audit_envelopes": "test_runner_default.data.passed=true (1 test); test_runner_invariants.data.passed=true (3 tests, failed_tests=[])."
}
```

This is stronger than canonical-002's reviewer evidence — the
`audit_envelopes` field cites the specific upstream tool envelopes
the reviewer relied on, which is exactly the trail the audit tool
nodes were added to produce.

## What worked (and what changed since canonical-003)

This run is the third iteration on the audit-node prompt path:

| run            | DAG shape                                  | result            | issue                                                   |
|----------------|--------------------------------------------|-------------------|---------------------------------------------------------|
| canonical-002  | analyzer / implementer / reviewer (3-node) | done, 92/100      | no audit nodes; process_auditability capped at 10/15    |
| canonical-003  | + default_audit + invariant_audit (5-node) | halted on schema  | uniform `failed_tests: array` schema; default audit halt |
| canonical-005  | full 5-node, schemas matched per script    | **done, 97/100**  | (none)                                                  |

The two prompt-side changes that closed the gap:

1. **prompt_analyzer/SKILL.md** now surfaces
   `deterministic_test_scripts` as objects with `path` AND
   `envelope_data_fields` — so the designer sees what each script
   actually emits, not just its name.
2. **workflow_designer/SKILL.md** mandates "match the script's
   ACTUAL envelope; declare ONLY the fields it emits." Minimum-viable
   audit schema is `passed/tests_run/output`; `failed_tests: array`
   added only when the analyzer's `envelope_data_fields` confirms it.

Fixture is unchanged from canonical-002 (`run_default_tests.sh` and
`run_invariants.sh` still emit different shapes, which is fine — the
Planner now matches each).

## Remaining gap to 100

Only **robustness_minimality 7/10** is recoverable without forcing
retry or hardening the case. 50 diff lines for a 4-requirement CSV
parser is reasonable; chasing tighter code would trade clarity for a
3-point gain. Reviewer flagged this as do-not-chase. **97 is the
realistic first-pass max** for this fixture under current rubric.

(`evidence_quality` is at the cap; `process_auditability` is at the
cap; `resilience` is at the cap; `requirement_coverage` and
`test_correctness` are at the cap.)

## Files & artifacts

Run-dir layout:
```
.camflow/run/
├── prompt.txt                              # original user prompt
├── workflow.yaml                           # 5-node compiled DAG
├── trace.jsonl                             # 22 events
├── nodes/
│   ├── analyzer/attempt-1/
│   ├── implementer/attempt-1/              # incl. verify.command output
│   ├── test_runner_default/attempt-1/      # tool envelope: passed=true, tests_run=1, output=...
│   ├── test_runner_invariants/attempt-1/   # tool envelope: passed=true, tests_run=3, failed_tests=[], output=...
│   └── reviewer/attempt-1/                 # incl. per_requirement_evidence + audit_envelopes citations
└── planner/
    ├── workflow.yaml                       # = builtin/planner/workflow.yaml
    ├── trace.jsonl                         # 14 events (no retry on any planner node)
    └── nodes/{understand,design_dag,render_yaml}/attempt-1/
```

All artifacts preserved at
`/tmp/camflow-canonical-20260504-184701/` for post-mortem.
