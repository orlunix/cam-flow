# camflow project memory

Camflow is a small Python 3.6-compatible prompt-call-verify-trace runner for
checked-in v1.2 workflows. The current source of truth is
[`docs/design-v1.2.md`](docs/design-v1.2.md). [`docs/spec.md`](docs/spec.md)
documents the superseded v1.1 runtime and is historical only.

## Current architecture

The shipped runtime is `src/camflow_pkg/`, built into the readable
`dist/camflow` shell/Python polyglot by `build_camflow.py`.

```text
workflow.yaml + optional input.json
  -> validate static graph and local skills
  -> execute nodes serially in YAML order
  -> call one camc agent per node/verification
  -> validate envelope and deterministic verifier
  -> persist attempts, routes, trace, and agent archive
```

`src/runner/` and package-manager-era tests are retained migration/history
code. Do not extend them for v1.2 behavior.

## Load-bearing rules

1. Keep the runtime thin: no mutable global state, dynamic graph mutation,
   autonomous replanning, loops, server, registry, or plugin framework.
2. `input.json` is the immutable run-wide input. Node outputs flow only across
   declared `needs` edges through `upstream`.
3. Scheduling is serial and deterministic: the first ready node in YAML order
   runs next.
4. `needs` is the edge relation. Restricted `when` is the only router:
   `{node, path: data.<field>, equals}` against a direct dependency's declared
   string output. Exactly one branch must match.
5. Every attempt persists prompt, input, raw agent output, validated output,
   verification, route/skip evidence, and trace events.
6. Fresh runs reject non-empty run directories. `run.json` hashes the captured
   workflow and input; resume/run-from reject later mutations.
7. Real agents are launched only through `camc`, without auto-exit. Successful
   durability order is `archive -> status snapshot -> stop -> rm`. Archive
   failure halts and keeps the camc record.
8. Envelope status remains binary (`success` or `fail`). `skipped` is a
   scheduler state stored in `skip.json`, never an agent-produced envelope.
9. No parallelism and no implicit fallback behavior.

## Supported commands

```text
camflow plan <prompt> --out DIR
camflow pack DIR --out DIR
camflow run workflow.yaml [--input input.json] [--out RUN_DIR]
camflow batch workflow.yaml --inputs GLOB --out DIR
camflow resume RUN_DIR
camflow run --from NODE --run-dir RUN_DIR
```

## Verification

After runtime or contract changes, run at least:

```bash
python3 -m unittest tests.test_branch_routing tests.test_camc_lifecycle_v12 -v
python3 -m unittest tests.test_camflow_build -v
python3 build_camflow.py --output dist/camflow
python3 -m py_compile dist/camflow
./dist/camflow version
```

Use the mock `CAMFLOW_EXECUTOR` for local graph tests. A real camc/Codex smoke
test spends agent resources and should only be run when explicitly requested.

Do not push unless the user explicitly asks. Never force-push.
