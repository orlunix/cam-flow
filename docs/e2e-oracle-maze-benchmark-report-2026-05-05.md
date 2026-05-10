# Oracle Maze E2E Benchmark Report — CamFlow vs Single Agents

**Date:** 2026-05-05  
**Source:** local blind-maze oracle benchmark + CamFlow release validation  
**Primary run dir:** `/tmp/oracle-maze-fix-20260505-044946/maze/.camflow/run`  
**Decision:** fixed CamFlow auto-replan passes the release benchmark.

## Executive Summary

CamFlow now beats the best single-agent run on this benchmark because it solves
the maze and preserves the operational evidence that matters for release work:
halt classification, automatic Planner re-entry, DAG revision tracking,
bounded retry, and replayable artifacts.

The key result is:

- **Fixed CamFlow auto-replan:** 96 / 100
- **Best single-agent baseline:** 87 / 100 (`Codex /goal`)
- **Delta:** +9 points overall, or +17 points against the same Claude Opus 4.7
  model family baseline.

This is not a pure model-quality benchmark. It is a system benchmark: given a
controlled failure that requires replan and retry, does the agent setup finish
the job and leave enough evidence to trust the result?

## Scoreboard

| Rank | System | Score | Result | What It Proved |
| ---: | --- | ---: | --- | --- |
| 1 | Fixed CamFlow auto-replan | **96** | PASS | Solved with rev1 halt -> auto-replan -> rev2 retry -> replayable artifacts |
| 2 | Codex `/goal` pure single-agent | 87 | PASS | Best direct single-agent solve; no CamFlow runtime artifacts |
| 3 | Codex single-agent | 84 | PASS | Clean direct solve; manual revision handling |
| 4 | Claude single-agent | 79 | PASS | Solved, but with extra operational noise |
| 5 | CamFlow auto-replan before fix | 73 | FAIL final solve | Replanned correctly, but phrase-hint retry did not fire |

## Why This Benchmark Exists

The benchmark asks a narrow release question: is CamFlow's multi-agent/runtime
machinery meaningfully better than asking one strong agent to solve the same
task directly?

The decision criteria are:

- CamFlow must score higher than the best single-agent run.
- CamFlow must demonstrate value that a single agent does not naturally
  provide: explicit halt handling, automatic replan, DAG revision tracking,
  bounded retry, and a replayable artifact trail.

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

| Step | Expected Behavior |
| ---: | --- |
| 1 | Infer the hidden path from `maze_probe` responses. |
| 2 | Submit the path at `dag_revision=1`. |
| 3 | Treat the deliberate `ORACLE_HALT` as a workflow-level halt, not an ordinary retry. |
| 4 | Auto-replan to revision 2 when `on_halt: replan` is set. |
| 5 | Submit again at `dag_revision=2`. |
| 6 | Use bounded retry for recoverable `phrase_hint` feedback. |
| 7 | Verify `maze_status.data.solved == true`. |

Two constraints make this non-trivial:

- The first correct submit at `dag_revision=1` deliberately returns a halt /
  replan signal.
- A successful solve must happen at `dag_revision >= 2`; single agents can
  improvise that manually, but CamFlow must record and execute it as runtime
  state.

This shape is deliberately chosen to test the part of CamFlow that ordinary
coding benchmarks often miss: can the runtime own recovery instead of relying
on one long-running agent to improvise?

## Test Matrix

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

The fixed CamFlow run is the release-gating run. The earlier CamFlow run is
kept in the report because it explains the defect that was fixed: CamFlow could
replan, but it failed to use bounded retry for the rev2 phrase hint.

## Model / Agent Configuration

Model provenance was checked after the first report publication. The benchmark
mixed agent frontends and model families, so the scores below should be read as
system-plus-agent results, not as a strict same-model ablation.

| System | Agent Frontend | Model / Effort Recorded |
| --- | --- | --- |
| CamFlow auto-replan before fix | CamFlow runtime via `camc` child agents | `claude-opus-4-7`; Claude Code `2.1.128` |
| Fixed CamFlow auto-replan | CamFlow runtime via `camc` child agents | `claude-opus-4-7`; Claude Code `2.1.128` |
| Claude single-agent | Claude Code CLI | Opus 4.7, 1M context; Claude Code `2.1.128` |
| Codex single-agent | OpenAI Codex CLI | `gpt-5.5 xhigh fast`; Codex `v0.128.0` |
| Codex `/goal` pure single-agent | OpenAI Codex CLI with `/goal` | `gpt-5.5 xhigh fast`; Codex `v0.128.0`; unstable `goals` feature enabled |
| Codex `/goal` invalid run | OpenAI Codex CLI, then nested CamFlow | Not scored |

Evidence checked:

- CamFlow before fix: every archived child session under
  `/tmp/oracle-maze-benchmark-20260505-034751/camflow-auto/.../.camflow/run`
  has `agent.json.task.tool=claude` and `claude/session.jsonl`
  `message.model=claude-opus-4-7`.
- Fixed CamFlow: every archived child session under
  `/tmp/oracle-maze-fix-20260505-044946/maze/.camflow/run` has
  `agent.json.task.tool=claude` and `claude/session.jsonl`
  `message.model=claude-opus-4-7`.
- Claude single-agent transcript lines 3 and 12 show Claude Code `v2.1.128`
  and `Opus 4.7 (1M context)`.
- Codex single-agent transcript lines 20 and 22 show OpenAI Codex `v0.128.0`
  and `model: gpt-5.5 xhigh fast`.
- Codex `/goal` pure single-agent transcript lines 15 and 17 show Codex
  `v0.128.0` and `model: gpt-5.5 xhigh fast`; the transcript also records
  the under-development `goals` feature warning.
- Codex `/goal` invalid run is excluded because it invoked CamFlow and
  therefore was not a pure single-agent baseline.

Implications:

- Same-model comparison: fixed CamFlow (`claude-opus-4-7`) scored 96 versus
  Claude single-agent (`Opus 4.7`) at 79, a +17 delta.
- Best-baseline comparison: fixed CamFlow (`claude-opus-4-7`) scored 96 versus
  the best Codex `/goal` single-agent (`gpt-5.5 xhigh fast`) at 87, a +9
  cross-model/system delta.
- The report does not claim a pure model-quality comparison. It measures the
  operational system behavior available to each tested setup: runtime-owned
  halt classification, auto-replan, revision artifacts, bounded retry, and
  replayability.

## Scoring Method

Total score: 100 points.

The score is intentionally not just a `solved=true` check. A single agent can
solve the maze by manually changing `CAMFLOW_DAG_REVISION`, but CamFlow is
being judged on whether the runtime itself handles the failure mode and records
the resulting state transitions.

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

## Evidence Map

The release-gating CamFlow run produced the evidence we would want before
shipping an automation feature:

| Evidence | Location / Signal | Why It Matters |
| --- | --- | --- |
| Runtime status | `camflow status` on the fixed run dir | Shows final `success`, active DAG revision 2, and all nodes done |
| Revision archive | `dag_revisions/0001` and `dag_revisions/0002` | Proves the runtime created a new DAG revision instead of mutating history |
| Trace | `trace.jsonl` | Shows rev1 halt, auto-replan, rev2 retry, and final completion in order |
| Node attempts | `nodes/*/attempt-*` | Preserves each agent attempt and retry boundary |
| Oracle final state | `maze_status` from `confirm_solved` | Confirms `solved=true` and final submit at `dag_revision=2` |
| Deterministic tests | `tests/test_runtime.py` and full `tests/` suite | Guards the Phase B behavior outside the live oracle run |

## Detailed Run Notes

The sections below keep the raw operational detail for auditability. The short
version is in the scoreboard; this section explains what each system actually
did.

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
