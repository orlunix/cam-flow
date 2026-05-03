# camflow

> Self-hosting, prompt-driven multi-agent workflow runner.

```
camc    run "<prompt>"  →  one agent runs alone, hopes for the best
camflow run "<prompt>"  →  a builtin Planner workflow compiles the
                           prompt into a DAG of high-quality, verified
                           nodes — then a Runtime executes that DAG
```

camflow's user surface is the same shape as `camc run`: one mandatory
prompt, no flags to learn. The difference is what happens after — a
builtin Planner workflow turns the prompt into a `workflow.yaml` that
the runtime executes node-by-node, with retry + verify on every step.

The workflow.yaml is a compiler output, not something you author.

## Install

```bash
pip install -e .
```

This installs the `camflow` CLI pointing at `runner_v2.runtime:main`.

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
pytest tests/test_v2.py -q
```

## Docs

- [`docs/spec-v1.1.md`](docs/spec-v1.1.md) — language + runtime spec (source of truth).
- [`CLAUDE.md`](CLAUDE.md) — project memory for future Claude sessions.

## Status

v1.1 — self-hosting Planner, two classes (Workflow + Node), strict
contracts. See spec for the doctrine list.
