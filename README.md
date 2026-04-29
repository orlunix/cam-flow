# camflow

Minimal agent workflow runner.

> DAG for dependency modeling, serial runner for execution.

See [`docs/harness-design.md`](docs/harness-design.md) for design and
[`docs/spec.md`](docs/spec.md) for the workflow language spec.

## Install

```bash
pip install -e .
```

## Run

```bash
camflow examples/retry-demo/workflow.yaml \
    --state examples/retry-demo/state.json
```

## Test

```bash
pip install -e '.[test]'
pytest tests/
```
