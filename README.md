# camflow

A thin prompt-call-verify-trace runner for checked-in, static v1.2 workflows.

## Run

```bash
camflow run workflow.yaml --input input.json
camflow batch workflow.yaml --inputs "cases/*.json" --out runs/batch-001
camflow run --from analyze_lsu --run-dir runs/case-001
camflow resume runs/case-001 --feedback "check the timeout window"
```

`workflow.yaml` is the canonical input. `input.json` is read-only per-run context; when a workflow declares `input_schema`, `--input` is required. Planner support is optional and no v1.2 Planner adapter is currently configured.

See [`docs/design-v1.2.md`](docs/design-v1.2.md) for the specification.

## Install

```bash
pip install -e .
```

This installs the `camflow` CLI pointing at `runner.runtime:main`.

> **Source-tree-only for v1.1.** The runtime resolves `builtin/` and
> `skills/` as siblings of `src/`, so it expects the on-disk repo
> layout. Run via `pip install -e .` from a clone (editable install).
> Wheel/sdist packaging that ships `builtin/` and `skills/` as package
> data is deferred (see
> [`docs/camflow-asset-management-plan-001-2026-05-03.md`](docs/camflow-asset-management-plan-001-2026-05-03.md)
> §5 P3).

## Run

```bash
camflow run    "Fix the TypeError on line 87 of foo.py"   # fire-and-forget
camflow run -i "Fix the TypeError on line 87 of foo.py"   # pause for plan approval
camflow run --from <node_id>                              # re-execute a node + downstream
camflow run --steps 1 "<prompt>"                          # debug: halt after first attempt
camflow resume <run_dir>                                  # resume a halted run
```

The `-i` / `--interactive` flag pauses after Planner finishes
designing, so you can review (and revise) the compiled `workflow.yaml`
before the runtime executes it. `--from <node_id>` re-runs a specific
node (plus its downstream) on an existing `./.camflow/run/` —
operate-on-existing path, mutex with a fresh-run prompt.

Inspect a run while it's going:

```bash
cat .camflow/run/trace.jsonl                              # event stream
kill $(cat .camflow/run/runner.pid)                       # stop it
```

## Test

```bash
pip install -e '.[test]'
pytest tests/test_runtime.py -q
```

## Docs

- [`docs/spec.md`](docs/spec.md) — language + runtime spec (source of truth).
- [`CLAUDE.md`](CLAUDE.md) — project memory for future Claude sessions.

## Status

**Version 1.1** — self-hosting Planner, two classes (Workflow +
Node), strict contracts. See [`docs/spec.md`](docs/spec.md) for the
doctrine list.
