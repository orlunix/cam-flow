# camflow

A thin prompt-call-verify-trace runner for checked-in, static v1.2 workflows.

## Run

```bash
camflow run workflow.yaml --input input.json
camflow batch workflow.yaml --inputs "cases/*.json" --out runs/batch-001
camflow run --from analyze_lsu --run-dir runs/case-001
camflow resume runs/case-001 --feedback "check the timeout window"
```

`workflow.yaml` is the canonical input. `input.json` is read-only per-run
context; when a workflow declares `input_schema`, `--input` is required.
Planner support is optional; no v1.2 Planner adapter is configured yet.

## Install

```bash
pip install -e '.[test]'
```

CamFlow currently resolves checked-in `skills/` from the source tree, so use
an editable install from a clone.

## Test

```bash
pytest tests/test_runtime.py tests/test_v12.py -q
```

## Docs

- [`docs/design-v1.2.md`](docs/design-v1.2.md) — v1.2 source of truth.
- [`docs/spec.md`](docs/spec.md) — historical v1.1 implementation reference.
