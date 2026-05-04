# value-demo — A/B harness (single camc agent vs camflow)

A small, auditable comparison rig. The same prompt
(`PROMPT.txt`) is run two ways against fresh isolated copies of
`fixture/`, then both sides are scored with a 100-point rubric.

The point isn't "camflow always wins." The point is: even when both
sides solve the case, the *structural* artifacts — DAG, per-attempt
outputs, trace events, retry-with-feedback, evaluator envelopes —
exist on the camflow side and don't exist on the baseline side. That
difference is what the rubric's auditability / evidence / recovery
rows reward.

---

## Layout

```
examples/value-demo/
├── README.md                      # this file
├── PROMPT.txt                     # the user-facing task (both sides see this verbatim)
├── AB-PROTOCOL.md                 # exact procedure for running both sides + scoring
├── workflow-reference.yaml        # what the builtin Planner is *expected* to produce
├── fixture/                       # pristine project the agent operates on
│   ├── SPEC.md                    # 4 numbered requirements (Req 1 visible test, Req 2-4 invariants)
│   ├── lib/csvparser.py           # stub that raises NotImplementedError
│   ├── lib/__init__.py
│   ├── tests/test_csvparser.py    # the single visible failing test (Req 1)
│   ├── tests/conftest.py          # adds project root to sys.path
│   ├── tests/invariants/          # the hidden invariant tests (Req 2-4)
│   │   ├── __init__.py
│   │   └── test_invariants.py
│   └── scripts/                   # tool/verify scripts referenced by workflow-reference
│       ├── run_default_tests.sh   # tool node: pytest tests/, emits envelope JSON
│       ├── run_invariants.sh      # tool node: pytest tests/invariants/, emits envelope JSON
│       └── run_all_tests.sh       # implementer.verify.command: bash, exits non-zero on any fail
└── scripts/                       # harness scripts (run from the camflow repo)
    ├── setup-fixture.sh           # cp -r fixture/ <fresh-dest> (refuses to overwrite)
    └── score.py                   # reads a finished run dir → 100-point rubric JSON
```

The fixture is intentionally tiny (~50 LOC stub + 4-line spec + 4
tests) so anyone can read it in 60 seconds and see that the scoring
isn't rigged.

---

## Quick start

```bash
cd <camflow-repo>
PFX=/tmp/ab-$(date +%Y%m%d-%H%M%S)

# baseline
bash examples/value-demo/scripts/setup-fixture.sh "$PFX/baseline"
( cd "$PFX/baseline"
  camc run --path "$PFX/baseline" --name value-demo-baseline \
    "$(cat $OLDPWD/examples/value-demo/PROMPT.txt)" )

# camflow
bash examples/value-demo/scripts/setup-fixture.sh "$PFX/camflow"
( cd "$PFX/camflow"
  camflow run "$(cat $OLDPWD/examples/value-demo/PROMPT.txt)" )

# score
python examples/value-demo/scripts/score.py "$PFX/baseline" \
  > "$PFX/baseline.score.json"
python examples/value-demo/scripts/score.py "$PFX/camflow" \
  > "$PFX/camflow.score.json"
```

Full procedure (incl. transcript capture, what-to-look-at lists, and
the result-table template): see [`AB-PROTOCOL.md`](AB-PROTOCOL.md).

The 100-point rubric itself lives in
[`docs/e2e-ab-score-protocol-codex-2026-05-04.md`](../../docs/e2e-ab-score-protocol-codex-2026-05-04.md).

---

## CI / deterministic check

Manual A/B runs cost LLM credits and are non-deterministic. For CI we
have a hermetic E2E that drives `workflow-reference.yaml` directly via
`run_workflow()` with `camc.run_and_collect` monkey-patched to return
canned envelopes:

```bash
PYTHONPATH=src pytest tests/test_e2e_value.py -q
```

That test asserts the runtime: enforces the auto-schema, fires
`retry_triggered` when the implementer's verify command fails, injects
`previous.feedback` on the next attempt, and writes per-attempt
artifacts (`output.json`, `prompt.txt`, `agent_output.json`) to
`<run>/nodes/<id>/attempt-N/`. **No LLM is invoked.**
