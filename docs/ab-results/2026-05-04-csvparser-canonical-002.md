# A/B Result — value-demo Canonical Run 002 — 2026-05-04

> ✅ **Successful canonical CamFlow run.** First-pass `done` lifecycle,
> all 4 SPEC requirements satisfied, all 4 tests pass (1 visible +
> 3 invariants = 4 total), reviewer evidence per-requirement.
> **Score: 92/100.**

## Run metadata

- camflow repo: `/home/hren/.openclaw/workspace/camflow` @ commit
  `665a5c4` (resilience semantics + Planner softening committed in
  the previous round).
- cam: `/home/hren/.openclaw/workspace/cam` @ tip = `31d8278`
  (committed) plus cam-dev's **uncommitted local fix** for
  `tmux_send_input` chunking — the `_TMUX_SEND_CHUNK = 8192` change
  in `src/camc_pkg/transport.py` and the rebuilt `dist/camc` →
  `~/.cam/camc`. Without that local fix this run would have halted
  at `design_dag` again as in canonical-001.
- Deployed `camc`: `/data/venv/bin/camc`, v1.2.0 (dev). Editable
  install picks up the source change immediately; bundle at
  `~/.cam/camc` also resynced.
- Claude Code: v2.1.126.
- Fresh fixture: `/tmp/camflow-canonical-20260504-085238/camflow`
  (10 files; setup-fixture.sh refused to overwrite, fresh path).
- Run dir:
  `/tmp/camflow-canonical-20260504-085238/camflow/.camflow/run/`.

## Command

```bash
cd /home/hren/.openclaw/workspace/camflow
PFX=/tmp/camflow-canonical-20260504-085238    # date-stamped fresh path
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

Wall-clock: ~13 minutes (Planner 8min + user workflow 5min).

## Generated DAG (Planner output)

The compiled `workflow.yaml` has **3 user nodes**:

| id          | run                | needs                  | retry | verify                                               |
|-------------|--------------------|------------------------|-------|------------------------------------------------------|
| analyzer    | skill: analyzer    | —                      | 2     | (default agent verify against steps)                 |
| implementer | skill: code_writer | [analyzer]             | 3     | command: walk-up to SPEC.md → bash run_all_tests.sh  |
| reviewer    | skill: reviewer    | [analyzer, implementer]| 2     | (default agent verify against steps)                 |

Planner picked the right skill names (`analyzer`/`code_writer`/`reviewer`),
generated a cwd-safe `verify.command` with the walk-up-to-SPEC.md
pattern (anchored on the user-named marker, exit-2 guard included),
and configured bounded retry on every skill node.

**Planner did NOT generate explicit `test_runner` / `invariant_checker`
audit tool nodes** even though the verbatim 5-node template in
`workflow_designer/SKILL.md` shows them and the project has
deterministic envelope-emitting scripts (`scripts/run_default_tests.sh`,
`scripts/run_invariants.sh`). This is the structural gap remaining
for the >96 target — see "Next blockers" below.

## Trace (top-level user workflow)

```
node_started analyzer attempt-1 (15:00:19) → verify_completed → node_completed (16:01:48, success)
node_started implementer attempt-1 (16:01:48) → verify_completed → node_completed (16:03:32, success)
node_started reviewer attempt-1 (16:03:32) → verify_completed → node_completed (16:05:18, success)
workflow_completed status=success
```

`retry_triggered` events: **0**. First-pass success across all 3 nodes.

Planner sub-trace (understand → design_dag → render_yaml) similarly
ran clean: each first-pass success, no retry.

## Tests on the produced implementation

There are **4 tests total** in the fixture: 1 visible
(`tests/test_csvparser.py::test_basic_split`) + 3 invariant
(`tests/invariants/test_invariants.py::test_strip_surrounding_whitespace`,
`::test_quoted_field_with_comma`,
`::test_doubled_quote_inside_quoted_field`). All pass on the
generated implementation.

```
$ cd /tmp/camflow-canonical-20260504-085238/camflow
$ bash scripts/run_all_tests.sh
....                                                                     [100%]
4 passed in 0.01s

$ pytest tests/invariants/ -q --tb=short
...                                                                      [100%]
3 passed in 0.01s
```

`score.py`'s `tests_visible.count = 1` reflects `pytest tests/
--ignore=tests/invariants` (the visible suite alone, 1 test).
`tests_invariants.count = 3` reflects `pytest tests/invariants/`.
1 + 3 = 4 unique tests, matching the `run_all_tests.sh` "4 passed".

Diff size vs pristine: **50 lines** (lib/csvparser.py only — the
single 53-line implementation replaces the 1-line stub).

Scope: only `lib/csvparser.py` was modified. No tests, scripts,
SPEC.md, or other files touched (verified via `find -newer`).

## Score table (rubric per `scripts/score.py` + manual rows)

| category                | weight | score | source / rationale                                                                                       |
|-------------------------|--------|-------|----------------------------------------------------------------------------------------------------------|
| requirement_coverage    | 35     | 35    | auto: 1 visible + 3 invariant tests pass, all 4 SPEC reqs satisfied                                      |
| test_correctness        | 20     | 20    | auto: visible suite + invariants both green                                                              |
| evidence_quality        | 15     | 15    | manual: reviewer envelope has `per_requirement_evidence` map with file:line + passing test for req1..req4 |
| process_auditability    | 15     | 10    | auto: 3 user nodes / 14 trace events / 3 attempts. Capped because Planner skipped audit tool nodes        |
| robustness_minimality   | 10     | 7     | auto: diff_lines=50 → "≤80" bucket                                                                       |
| resilience              |  5     |  5    | auto: lifecycle done + bounded retry configured on all 3 skill nodes; retry_triggered=0 (no churn)       |
| **TOTAL**               | **100**| **92**|                                                                                                          |

`score.py` JSON lives at:
`/tmp/camflow-canonical-20260504-085238/camflow/.camflow/run/` (run
dir — re-run via `python examples/value-demo/scripts/score.py
/tmp/camflow-canonical-20260504-085238/camflow`).

## What worked well

- **First-pass correctness end-to-end.** No retry was needed; this is
  the ideal outcome per the reviewer-corrected resilience semantics.
- **Bounded retry CONFIGURED** on every skill node (analyzer 2,
  implementer 3, reviewer 2) — recovery readiness without churn,
  full 5/5 resilience.
- **Deterministic gate via `verify.command`.** Implementer's verify
  walked up to SPEC.md and ran `bash scripts/run_all_tests.sh`;
  exit 0 confirmed all 4 tests pass before the implementer node
  could be marked success.
- **Per-requirement reviewer evidence.** Reviewer's envelope
  includes a `per_requirement_evidence: {req1, req2, req3, req4}`
  map with each entry naming a `file:line` range AND a passing test
  name. Plus a `scope_check` confirming only `lib/csvparser.py`
  changed. This is the strong-evidence shape the strengthened
  reviewer SKILL.md asked for.
- **Generated `verify.command` is cwd-safe.** The walk-up-to-marker
  pattern from `workflow_designer/SKILL.md` was followed verbatim
  (anchored on `SPEC.md`, exit-2 guard present).
- **`output_schema` types are clean.** Every node uses only the five
  legal type names — no `bool`/`int`/`array of <X>` leaks.

## Remaining blockers for >96

### 1. (MAJOR) Planner skipped explicit audit tool nodes

The verbatim 5-node template in `workflow_designer/SKILL.md`
includes `test_runner` and `invariant_checker` as `run.tool` nodes
that emit structured pass/fail envelopes. The project even ships
matching scripts (`scripts/run_default_tests.sh`,
`scripts/run_invariants.sh`) that emit the right envelope shape.
But Planner produced only `analyzer`/`implementer`/`reviewer` and
folded the test-result audit into the implementer's `verify.command`
and the reviewer's prose-level test-name citations.

**Cap:** process_auditability hits 10/15 because the trace doesn't
have separate audit envelopes from the tool nodes; the per-class
test evidence is implicit (passing pytest run) rather than
explicit (structured envelope per audit node).

**Fix candidate** (next round, if scoped): tighten
`workflow_designer/SKILL.md` to make audit nodes **mandatory** when
the project ships deterministic envelope-emitting test scripts.
Two prompts changes:

1. `prompt_analyzer/SKILL.md` could surface a new field
   `deterministic_test_scripts: array` listing any `scripts/run_*tests*.sh`
   (or `make test`, etc.) found in the project. This gives the
   designer a structured handle to wire into `run.tool`.
2. `workflow_designer/SKILL.md` adds a checklist near the recipe:
   "If `upstream.understand.data.deterministic_test_scripts` is
   non-empty, you MUST include one audit tool node per script
   (using `run.tool: <script_path>`)."

This stays prompt-only, no spec change, no new skills, no fixture
change. The reviewer's "small repo-provided generic tool wrapper"
suggestion is already met by the existing fixture scripts; the
gap is just teaching the Planner to use them.

### 2. (MINOR) Diff size 50 lines → robustness 7/10 instead of 10/10

50 lines for a 4-requirement CSV parser is reasonable; the
implementation handles each requirement explicitly (no clever
golfed code). Three points are recoverable if a future
implementer happens to write a tighter parser, but optimizing for
diff size at the cost of clarity is the wrong trade. The reviewer
flagged this as do-not-chase. Leaving it.

### 3. (REPRO RISK) cam tmux-chunking fix is locally uncommitted

The chunking patch in cam's `src/camc_pkg/transport.py` and the
rebuilt `dist/camc` → `~/.cam/camc` are **uncommitted on cam's
working tree** at the time of this run. cam-dev surfaced this for
human commit decision (one combined commit vs. two separate). If
that working-tree state is reverted, this canonical run becomes
non-reproducible — the next attempt would halt at `design_dag` as
in canonical-001. Suggest cam-dev land the commit before any
further reruns.

## Comparison vs. previous canonical-001

| dimension                   | canonical-001 (halted)             | canonical-002 (this run, ✅) |
|-----------------------------|------------------------------------|-----------------------------|
| Planner `understand`        | success                            | success                     |
| Planner `design_dag`        | hung — 20KB prompt → tmux ARG_MAX  | success                     |
| Planner `render_yaml`       | (never reached)                    | success                     |
| User workflow.yaml produced | no                                 | yes (3 nodes)               |
| User nodes completed        | 0                                  | 3/3 first-pass              |
| Tests pass                  | n/a                                | 4/4                         |
| Score                       | n/a (aborted)                      | **92/100**                  |
| Cleared blockers            | trust dialog, TOML parser          | + tmux send-keys chunking   |

Three layers of cam plumbing fixes were required to get from the
provisional 85 (run-001, via `python -m runner.runtime`) to a real
canonical 92: trust dialog `[[confirm]]` rule (`2caca9c`), TOML
array parser (`6ebba2c`), tmux send-keys chunking (uncommitted local).

## Files & artifacts

Run-dir layout:
```
.camflow/run/
├── prompt.txt                          # original user prompt
├── workflow.yaml                       # 3-node compiled DAG
├── trace.jsonl                         # 14 events
├── nodes/
│   ├── analyzer/attempt-1/             # input.json, prompt.txt, agent_output.json, output.json, verify/
│   ├── implementer/attempt-1/          # ditto, plus agent_output.json from verify.command run
│   └── reviewer/attempt-1/             # ditto, with per_requirement_evidence in output.json
└── planner/
    ├── workflow.yaml                   # = builtin/planner/workflow.yaml
    ├── trace.jsonl
    └── nodes/{understand,design_dag,render_yaml}/attempt-1/
```

All artifacts preserved at
`/tmp/camflow-canonical-20260504-085238/` for post-mortem review.
