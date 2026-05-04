# A/B comparison protocol — value-demo

This file documents the exact procedure for running both sides of an
A/B comparison (single-camc baseline vs. camflow) against the
csvparser fixture. Both sides receive the **same** prompt
(`PROMPT.txt`) and operate on a fresh isolated copy of `fixture/`.

The 100-point rubric is in [`docs/e2e-ab-score-protocol-codex-2026-05-04.md`](../../docs/e2e-ab-score-protocol-codex-2026-05-04.md);
`scripts/score.py` automates the objective rows.

---

## What gets compared

* **SINGLE_AGENT_BASELINE**: a single `camc run "$(cat PROMPT.txt)"`
  starts one Claude agent in a fresh isolated copy of the fixture.
  No role hints, no rubric leakage, no DAG decomposition. The agent
  has full file access and standard tooling.

* **CAMFLOW_RUN**: `camflow run "$(cat PROMPT.txt)"` in another fresh
  isolated copy of the fixture. The builtin Planner produces a DAG
  (compare to `workflow-reference.yaml` for the intended shape) which
  the runtime then executes.

Both sides see SPEC.md, the failing visible test, the `tests/invariants/`
directory, and the lib/ stub. Neither side gets the rubric.

---

## Prerequisites

* `camflow` installed (`pip install -e .` from the camflow repo).
* `camc` installed and configured.
* `pytest` available.
* `jq` available (needed for the verify=command checks in the
  reference workflow).

The harness does NOT auto-clean its output dirs. Pick fresh paths
(`/tmp/ab-<timestamp>/...`) so a new run never collides with an old
one — that's how we accumulate auditable result history.

---

## Canonical CamFlow invocation

The CamFlow leg **MUST** be launched via the user-facing
`camflow run` CLI, from inside the fixture-copy directory. Direct
runtime invocations (`python -m runner.runtime`, importing
`run_workflow` from a script, etc.) bypass the user-facing entry
point and produce results that are NOT comparable to a real-user
A/B. Such results should be marked **provisional** in the result
doc and not used as canonical scores.

The deterministic E2E in `tests/test_e2e_value.py` *does* drive the
runtime directly — that's intentional, it's a CI smoke test, not an
A/B leg.

---

## Run

```bash
cd <camflow-repo>

# Pick a fresh prefix per A/B session.
PFX=/tmp/ab-$(date +%Y%m%d-%H%M%S)

# 1. SINGLE_AGENT_BASELINE
bash examples/value-demo/scripts/setup-fixture.sh "$PFX/baseline"
( cd "$PFX/baseline"
  camc run --path "$PFX/baseline" --name "value-demo-baseline" \
    "$(cat $OLDPWD/examples/value-demo/PROMPT.txt)" )
# wait for the agent to finish; capture its tmux when ready:
camc capture <agent-id> > "$PFX/baseline.transcript"

# 2. CAMFLOW_RUN
bash examples/value-demo/scripts/setup-fixture.sh "$PFX/camflow"
( cd "$PFX/camflow"
  camflow run "$(cat $OLDPWD/examples/value-demo/PROMPT.txt)" )
# camflow returns when the workflow finishes (or halts).

# 3. SCORE BOTH SIDES
python examples/value-demo/scripts/score.py "$PFX/baseline" \
  > "$PFX/baseline.score.json"
python examples/value-demo/scripts/score.py "$PFX/camflow"  \
  > "$PFX/camflow.score.json"

# 4. Inspect
cat "$PFX/baseline.score.json"
cat "$PFX/camflow.score.json"
diff <(cd "$PFX/baseline" && find . -name '*.py' | sort) \
     <(cd "$PFX/camflow" && find . -name '*.py' | sort)

# 5. Manual rows: open the score JSON for each side and fill the
# evidence_quality.manual_pts (and recovery.manual_pts for baseline).
```

---

## What to capture, where

For SINGLE_AGENT_BASELINE:

* `<dest>/lib/csvparser.py` — agent's diff
* `<dest>/<any agent-created files>` — diff against pristine fixture
* `<dest>.transcript` — `camc capture <id>` output (full tmux)

For CAMFLOW_RUN:

* `<dest>/lib/csvparser.py` — same diff target
* `<dest>/.camflow/run/workflow.yaml` — what Planner compiled
* `<dest>/.camflow/run/trace.jsonl` — event stream (machine-readable)
* `<dest>/.camflow/run/nodes/<id>/attempt-N/` — per-attempt:
  * `prompt.txt` — what was sent to the skill agent
  * `agent_output.json` — what the agent produced
  * `output.json` — runtime-validated envelope
  * `verify/agent_output.json` — verify-agent's data (when applicable)
* `<dest>/.camflow/run/halt.json` — only present if halted
* `<dest>/.camflow/run/planner/` — Planner's own sub-run dir (recursive
  shape; same artifacts apply)

`scripts/score.py` reads these post-hoc.

---

## Score table template

After scoring both sides, write a result file at
`docs/ab-results/<ISO-timestamp>.md`:

```markdown
# A/B result — <ISO timestamp>

Model:       <e.g. claude-opus-4-7>
Camflow ver: <git rev-parse --short HEAD>
Prompt:      examples/value-demo/PROMPT.txt
Pristine:    examples/value-demo/fixture/

| category               | weight | baseline | camflow | evidence path                                      |
|------------------------|--------|----------|---------|----------------------------------------------------|
| requirement coverage   | 35     | <auto>   | <auto>  | score.py: tests_visible + tests_invariants pass    |
| test correctness       | 20     | <auto>   | <auto>  | score.py: pytest exit codes                        |
| evidence quality       | 15     | <manual> | <manual>| transcript / trace.jsonl + verify envelopes        |
| process auditability   | 15     | <auto>   | <auto>  | score.py: trace_events + attempts_total            |
| robustness/minimality  | 10     | <auto>   | <auto>  | score.py: diff_lines                               |
| resilience             |  5     | <manual> | <auto>  | first-pass done + bounded retry configured = 5/5;  |
|                        |        |          |         | clean halt + feedback = 4; done w/o retry = 3      |
| TOTAL                  | 100    |          |         |                                                    |

## Delta and why
<1-3 sentences explaining the structural difference, citing
specific artifact paths.>
```

The CamFlow advantage is **structural**: planning, durable
intermediate artifacts, non-self verification with evidence, and
bounded retry-with-feedback configured as a recovery safety net are
observable on disk. A given baseline run may score high on
(test_correctness, robustness) by happening to do the right thing —
that's fine, expected, and noted in the rubric. The auditability +
evidence + resilience rows always favor the structural runner because
the artifacts simply don't exist on the baseline side.

**Note on resilience scoring (per `codex-retry-semantics-correction`):**
Retry is a bounded safety net, NOT a positive target. The ideal
CamFlow run is first-pass correct with bounded retry *configured*
where appropriate — it gets full 5/5 even if no retry ever fires. A
clean halt with actionable feedback (operator can `camflow resume
--feedback`) gets 4/5. A halt without feedback or excessive retry
churn is penalized. We do NOT harden the fixture or design workflows
to manufacture retry events.

---

## What this is NOT

* Not a claim that single agents cannot solve the case. Some
  baseline runs will pass all 4 reqs; the rubric still differentiates
  them on auditability and recovery.
* Not a one-shot benchmark. Run 3 times per side, report median, keep
  raw artifacts so claims can be re-audited.
* Not a closed test suite. The fixture is small (≤ 50 LOC stub +
  4-line spec); deliberately so anyone can read it and verify the
  scoring is fair.

---

## Planner expectations for this fixture

When `camflow run` compiles `PROMPT.txt` against this fixture, the
Planner should produce a workflow whose **shape** is close to
`workflow-reference.yaml`. Specifically, on the camflow leg's
`<dest>/.camflow/run/workflow.yaml` we expect to see:

* **An analyzer-style first node** that reads `SPEC.md` + the test
  files and emits a structured requirement list. `prompt_analyzer`
  should have surfaced the test paths in `task_statement` /
  `test_files`, so the designer should not skip this.
* **An implementer node** with `verify.command` running the full
  deterministic test invocation (visible + invariants together —
  e.g. `pytest tests/ tests/invariants/` or `bash scripts/run_all_tests.sh`).
  This is what makes a missed Req 2/3/4 trigger retry-with-feedback
  rather than a hollow self-approval.
* **At least one audit tool node** that runs pytest and emits a
  structured envelope (`data.passed`, `data.tests_run`,
  `data.failed_tests`). The reference DAG splits this into two —
  `test_runner` for `tests/` and `invariant_checker` for
  `tests/invariants/` — but a single combined audit node is also
  acceptable.
* **A reviewer node** that depends on the analyzer + implementer + the
  audit node(s), and is told to cite per-requirement evidence.

The Planner is an LLM, not a deterministic compiler — exact node
names, counts, and `retry:` values will vary run-to-run. What we're
checking is whether the structural shape lines up. If the produced
workflow skipped the audit tool node, used `verify: agent` on the
implementer despite the deterministic test command being available,
or collapsed everything into 2-3 generic nodes — flag it as Planner
drift and re-run; if it persists, treat it as a Planner-prompt
regression.

The diagnostic mapping below covers what to do when the runtime
itself surprises you on a given run.

---

## Diagnostic mapping (when the result is surprising)

If CAMFLOW halts at Planner → planner DAG bad / planner skill weak
(check `<dest>/.camflow/run/planner/halt.json`).

If CAMFLOW completes but invariant_checker.passed=false →
implementer's verify either didn't run pytest invariants/ (workflow
shape bug — compare to workflow-reference.yaml) or ran but the
implementer skill couldn't recover after retry exhaustion (skill
quality issue — read `nodes/implementer/attempt-*/output.json`).

If CAMFLOW reviewer approved a diff that fails invariant_checker →
verify-agent shape check failed to catch a hollow approval (runtime
bug; runtime should reject malformed evaluator data per a3d69da).

If single baseline scores higher than camflow on (req_coverage,
test_correctness) → the case allowed an easy 1-shot solve; not a bug.
The structural rows (auditability, evidence, recovery) still
differentiate.

If retry_triggered events never fire under camflow despite the
implementer producing wrong code → runtime plumbing regression
(check that verify_with_command actually ran: look at
`nodes/implementer/attempt-1/agent_output.json`).
