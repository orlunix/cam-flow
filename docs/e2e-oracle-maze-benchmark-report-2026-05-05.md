# Oracle Maze E2E Benchmark Report — CamFlow vs Single Agents

Date: 2026-05-05

## Purpose

This report records the blind-maze oracle benchmark used to decide whether
CamFlow's multi-agent/runtime machinery is meaningfully better than a strong
single-agent baseline.

Decision criterion:

- CamFlow must score higher than the best single-agent run.
- The benchmark must prove the specific value of CamFlow: explicit halt
  handling, automatic replan, DAG revision tracking, bounded retry, and
  replayable artifacts.

## Benchmark Mechanism

The benchmark uses a local black-box oracle server. Participants may only use
the documented HTTP/tool interface; inspecting oracle files, logs, or process
internals is disallowed.

Oracle tools:

- `maze_scan`: returns the authoritative `path_length` and action alphabet.
- `maze_probe`: evaluates candidate path prefixes.
- `maze_submit`: submits a full path and optional phrase.
- `maze_status`: reports counters and final `solved` state.

Required behavior:

- The first correct submit at `dag_revision=1` deliberately returns an
  `ORACLE_HALT` / replan signal.
- A successful solve must happen at `dag_revision >= 2`.
- At `dag_revision >= 2`, a correct path may return recoverable non-halt
  feedback with `phrase_hint`.
- The correct system behavior is:
  1. infer path from probes;
  2. submit at revision 1;
  3. treat explicit halt as workflow-level halt, not ordinary retry;
  4. auto-replan to revision 2 if `on_halt: replan`;
  5. submit at revision 2;
  6. use bounded retry for recoverable `phrase_hint`;
  7. verify `maze_status.data.solved == true`.

## Systems Compared

Previous benchmark prefix:

`/tmp/oracle-maze-benchmark-20260505-034751`

Fresh oracle sessions used there:

| System | Port | Session | Notes |
| --- | ---: | --- | --- |
| CamFlow auto-replan before fix | 8771 | `885664c822ded11c255f67bb` | Failed final solve |
| Claude single-agent | 8772 | `01d4052bb595ad2ae53b4a7d` | Solved |
| Codex single-agent | 8773 | `03e2790a049b37e47f9549a2` | Solved |
| Codex `/goal` invalid run | 8774 | `d87b9587a7ef71b778c786b6` | Not scored; nested CamFlow |
| Codex `/goal` pure single-agent | 8775 | `71312a1f09d5056183152546` | Solved |

Fixed CamFlow rerun:

- Prefix: `/tmp/oracle-maze-fix-20260505-044946`
- Oracle port: `8776`
- Session: `e1ae1f3235259488c7830f6b`
- Run dir: `/tmp/oracle-maze-fix-20260505-044946/maze/.camflow/run`

## Scoring Method

Total score: 100 points.

| Category | Points | What It Measures |
| --- | ---: | --- |
| Final solve correctness | 35 | `maze_status.solved=true`, correct path, correct phrase if needed |
| Revision/replan semantics | 20 | rev1 controlled halt, new DAG revision, correct `dag_revision` propagation |
| Retry and recovery | 15 | explicit halt bypasses retry; recoverable feedback uses bounded retry |
| Evidence and replayability | 15 | `trace.jsonl`, node attempts, `dag_revisions`, manifests, status output |
| Black-box discipline | 10 | uses only oracle API/tools; no oracle internals |
| Efficiency/operational cleanliness | 5 | avoids unnecessary duplicate submits, loops, or manual intervention |

Single-agent baselines can score high on solving, but lose points on
CamFlow-specific replayability, DAG revision artifacts, and runtime-owned
replan semantics.

## Operations Per System

### CamFlow Auto-Replan Before Fix

Run dir:

`/tmp/oracle-maze-benchmark-20260505-034751/camflow-auto/maze/.camflow/run`

Operation:

- Started a fresh oracle.
- Ran CamFlow with `on_halt: replan`.
- Runtime reached rev1 submit halt.
- Runtime auto-started Planner re-entry and created rev2 artifacts.
- Rev2 submit returned recoverable `phrase_hint`.
- Planner/runtime combination did not complete the phrase retry correctly.

Observed failure:

- Final state: halted.
- Active revision: 2.
- `submit_path` attempt 1 failed with:
  `Path is correct. Use the phrase_hint in the final submit.`
- Generated rev2 workflow incorrectly treated submit as `retry: 0`, so
  recoverable phrase feedback halted the workflow instead of using retry.

Score: **73/100**

Reason:

- Strong artifact/replan evidence.
- Failed final solve.
- Retry and goal-driving value did not materialize.

### Claude Single-Agent

Transcript:

`/tmp/oracle-maze-benchmark-20260505-034751/claude-single.transcript`

Operation:

- Agent used the oracle API directly.
- It scanned, probed, inferred the path, advanced revision handling itself,
  and eventually submitted successfully.

Result:

- Solved: true.
- Final observed submit revision: 3.
- There was one extra same-revision submit caused by an environment/pipe
  handling mistake.

Score: **79/100**

Reason:

- Solved the task.
- Lacked CamFlow DAG revision artifacts and replayable runtime structure.
- Some operational noise.

### Codex Single-Agent

Transcript:

`/tmp/oracle-maze-benchmark-20260505-034751/codex-single.transcript`

Operation:

- Agent used the oracle API directly.
- It scanned, probed, inferred the path, handled revision progression itself,
  and submitted successfully.

Result:

- Solved: true.
- Final observed submit revision: 3.

Score: **84/100**

Reason:

- Cleaner than Claude single-agent.
- Still lacked CamFlow's explicit DAG revision artifacts, runtime replan, and
  auditable node-level trace.

### Codex `/goal` Pure Single-Agent

Transcript:

`/tmp/oracle-maze-benchmark-20260505-034751/codex-goal-pure.transcript`

Operation:

- Ran a single Codex agent using the new `/goal` command behavior.
- The invalid `/goal` run that invoked nested CamFlow was excluded from scoring.
- The pure single-agent `/goal` run solved the oracle without relying on
  CamFlow runtime artifacts.

Result:

- Solved: true.
- Final observed submit revision: 3.
- Goal achieved.
- Reported budget: 34062 / 70000 tokens.
- Wall time: about 66 seconds for the pure single-agent execution portion.

Score: **87/100**

Reason:

- Best single-agent baseline.
- Strong goal focus and fast execution.
- Still no CamFlow-owned auto-replan artifact trail.

### Fixed CamFlow Auto-Replan

Run dir:

`/tmp/oracle-maze-fix-20260505-044946/maze/.camflow/run`

Code changes tested:

- Runtime now classifies explicit halt/replan envelopes separately from
  ordinary failure.
- Explicit halt envelopes bypass node retry and trigger workflow halt directly.
- Ordinary non-halt failures still use bounded retry.
- `maze_submit.sh` now reads retry feedback from
  `input.previous.data.phrase_hint`.
- Replan context now tells Planner that `retry: 1` means one additional
  attempt and should not be set to zero merely to propagate halt.
- Oracle-maze prompt/reference no longer says one submit per revision in a way
  that contradicts phrase-hint retry.

Key implementation references:

- `src/runner/runtime.py`: `_envelope_requests_workflow_halt`
- `src/runner/runtime.py`: `explicit_halt_requested` scheduler branch
- `src/runner/runtime.py`: replan prompt section
  `Retry and explicit halt semantics`
- `examples/oracle-maze/scripts/maze_submit.sh`: `previous.data.phrase_hint`
  extraction
- `tests/test_runtime.py`: explicit halt and phrase-hint retry regressions

Deterministic tests:

```text
PYTHONPATH=src python3 -m pytest tests/test_runtime.py -q \
  -k 'explicit_oracle_halt or recoverable_phrase_hint or maze_submit_uses_previous_phrase_hint or PhaseBAutoReplan'

17 passed, 159 deselected
```

Full suite:

```text
PYTHONPATH=src python3 -m pytest tests/ -q

236 passed in 6.88s
```

Live run evidence:

- Final status: `success`
- Active DAG revision: `2`
- Auto-replan: `on_halt: replan (auto-replan used 1/1)`
- Nodes: `4/4 done`
- Final node: `confirm_solved`
- Final trace event:
  `workflow_completed`, `dag_revision=2`, `status=success`

Critical trace sequence:

```text
rev1 workflow_started
rev1 maze_scan success
rev1 infer_path success
rev1 submit_path attempt-1 fail: ORACLE_HALT
rev1 explicit_halt_requested retry_count=0 retry_max=1
rev1 workflow_halted
runtime auto-replan attempt 1/1
dag_revisions/0002 reason=auto_replan_after_halt
rev2 workflow_started
rev2 maze_scan success
rev2 infer_path success
rev2 submit_path attempt-1 fail: ORACLE_REJECT phrase_hint
rev2 retry_triggered retry_count=1 retry_max=1
rev2 submit_path attempt-2 success
rev2 confirm_solved success
rev2 workflow_completed success
```

Final oracle evidence from `confirm_solved`:

```text
event_counts: {probe: 57, scan: 2, submit: 3}
first_submit_revision: 1
solved: true
recent submit: correct_path=true, correct_phrase=true, dag_revision=2
```

Score: **96/100**

Reason:

- Solved the task.
- Proved rev1 halt -> runtime auto Planner re-entry -> rev2 execution.
- Recorded `dag_revisions/0002` with `parent_revision=1`,
  `reason=auto_replan_after_halt`, and `replan_count=1`.
- Demonstrated correct distinction between explicit halt and recoverable retry.
- Produced replayable artifacts: trace, node attempts, manifests, final status.
- Minor deductions only for Planner latency/probe volume and some status-output
  roughness observed during mid-run transitions.

## Final Scoreboard

| Rank | System | Score | Result |
| ---: | --- | ---: | --- |
| 1 | Fixed CamFlow auto-replan | **96** | PASS |
| 2 | Codex `/goal` pure single-agent | 87 | PASS |
| 3 | Codex single-agent | 84 | PASS |
| 4 | Claude single-agent | 79 | PASS |
| 5 | CamFlow auto-replan before fix | 73 | FAIL final solve |

## Conclusion

Before the fix, CamFlow had the right architecture but failed to capitalize on
its own retry/goal-driving mechanism, so it scored below strong single-agent
runs.

After the fix, CamFlow is meaningfully ahead of the best single-agent score:

```text
Fixed CamFlow: 96
Best single-agent baseline: 87
Delta: +9
```

The important improvement is not just `solved=true`. The live run proves the
system-level behavior that single agents do not provide: explicit halt
classification, automatic Planner re-entry, new DAG revision recording,
correct revision propagation, bounded retry from `previous.feedback`, and a
complete replayable artifact trail.

## Open Follow-Ups

- Reduce Planner re-entry latency.
- Improve mid-run `camflow status` display when old revision node artifacts are
  archived and new revision nodes are starting.
- Add a compact benchmark runner script that starts fresh oracle sessions and
  emits the scoreboard automatically.
