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

## Minimal graph model

- A node is one prompt-call-verify attempt with a typed output envelope.
- `needs` declares directed edges. A node sees successful outputs only from
  those direct dependencies; there is no mutable global data store.
- Top-level `input.json` is global only in the read-only sense: the same
  snapshot is injected into every node.
- `when` is the only router. It compares one direct dependency's declared
  string output with one literal value. Exactly one branch in a route group
  must match; otherwise the workflow halts instead of guessing.

The basic `test_or_dut` branch is:

```yaml
- id: test_or_dut
  output_schema:
    route: string
  # goal, steps, and run omitted here

- id: lsu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: lsu_debug}

- id: ifu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: ifu_debug}
```

The selected branch runs; the other branch gets a durable `skip.json`.
Downstream joins may depend on both branches: skipped dependencies count as
complete, but only the selected branch is injected into `upstream`.

## Replay and agent durability

Every run snapshots `workflow.yaml`, `input.json`, required skills, and their
workflow/input hashes in `run.json`. `resume` and `run --from` reuse that
snapshot and reject changed workflow/input files, making route decisions
replayable from the persisted node outputs and `trace.jsonl`.

For real camc agents, every attempt persists `agent.id`, `agent.json`, the
validated camc archive, and `camc-lifecycle.json`. Camflow does not use
auto-exit. Cleanup is strictly:

```text
archive -> status snapshot -> stop -> rm
```

If archive fails, Camflow halts and keeps the camc agent record instead of
removing the only deterministic session binding.

## Supervisor and CAMC identity

A supervisor agent may prepare `workflow.yaml` and `input.json`, invoke
Camflow, and handle `done` or `halted` results. The supervisor skill owns that
high-level policy; Camflow still owns node scheduling and child-agent cleanup
so retries, verifiers, resume, and replay cannot bypass the same rules.

The repository ships that policy as
[`supervisor-skills/managing-camflow/SKILL.md`](supervisor-skills/managing-camflow/SKILL.md).
It is intentionally outside `skills/`: the latter contains node executors and
is embedded into planner output, while `managing-camflow` is installed or
loaded only by the lifecycle supervisor.

Use a short, meaningful top-level workflow name such as `rvdbg`. Every CAMC
execution and verifier agent then receives:

```text
name: cf-<flow-label>-<node-label>-<attempt-hash>
tags: cf-<flow-label>, cf-<flow-id>
```

Labels are bounded and hash-suffixed when truncated. `run.json` persists the
eight-character run-level flow ID, so resume and `run --from` keep the same
tags. Use `camc list --tag cf-<flow-label>` to group a workflow name across
runs, or `camc list --tag cf-<flow-id>` to select one exact run.

## Test

```bash
python3 -m unittest tests.test_camflow_build -v
python3 build_camflow.py --output dist/camflow
python3 -m py_compile dist/camflow
```

## Docs

- [`docs/design-v1.2.md`](docs/design-v1.2.md) — v1.2 source of truth.
- [`docs/design-v1.2-appendix-plan-pack-run.md`](docs/design-v1.2-appendix-plan-pack-run.md) — plan/pack/run contract.
- [`supervisor-skills/managing-camflow/SKILL.md`](supervisor-skills/managing-camflow/SKILL.md) — supervisor authoring, monitoring, memory, and recovery policy.
