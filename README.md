# camflow

A thin prompt-call-verify-trace runner for checked-in, static v1.2 workflows.

## Build

```bash
python3 build_camflow.py
# or: scripts/build.sh
./dist/camflow version
```

`dist/camflow` is a readable shell/Python polyglot generated from
`src/camflow_pkg/`. It uses `${CAMFLOW_PYTHON:-python3}` and requires Python
3.6 or newer. It embeds every checked-in builtin skill and asset; `plan`
materializes editable copies, while `run` executes only the supplied package
or workflow files.

## Run

```bash
./dist/camflow plan "debug case_id=bug_001 sim_log=/runs/bug.log trace_log=/runs/trace.log" --out .camflow/plan/bug_001
./dist/camflow pack .camflow/plan/bug_001 --out packages/bug_001
./dist/camflow run packages/bug_001/workflow.yaml --input cases/bug_001.json --run-dir runs/bug_001
./dist/camflow batch packages/bug_001/workflow.yaml --inputs "cases/*.json" --out runs/batch-001
./dist/camflow resume runs/bug_001 --feedback "check the timeout window"
./dist/camflow run --from analyze_lsu --run-dir runs/bug_001
```

`input.json` is read-only per-run context; when a workflow declares
`input_schema`, `--input` is required. `run` rejects missing local skills and
never silently plans, packs, or generates them.

## Test

```bash
python3 -m unittest tests.test_camflow_build -v
python3 build_camflow.py --output dist/camflow
python3 -m py_compile dist/camflow
```

## Docs

- [`docs/design-v1.2.md`](docs/design-v1.2.md) — v1.2 source of truth.
- [`docs/design-v1.2-appendix-plan-pack-run.md`](docs/design-v1.2-appendix-plan-pack-run.md) — plan/pack/run contract.
