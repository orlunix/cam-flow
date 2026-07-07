# CamFlow Strategy — v1.2

> **Status: draft design, 2026-07-07.** This is the authoritative design for CamFlow v1.2. It supersedes the v1.1 stateful-graph, smooth-mode, and mandatory-Planner design. The normative detail is in [`design-v1.2.md`](design-v1.2.md).

## Position

CamFlow is a small **prompt-call-verify-trace runner** for checked-in agent and tool workflows. It is not an agent framework.

```text
Claude / Codex / deterministic tools = workers
CamFlow = deterministic runner + verifier + recorder
```

The v1.2 runtime is thin, deterministic, auditable, and recoverable. It executes immutable workflow YAML plus optional read-only `input.json` through a serial static `needs` DAG; it records per-attempt artifacts and an authoritative `trace.jsonl`.

## Canonical interfaces

```bash
camflow run <workflow.yaml> --input <input.json>
camflow plan "<prompt>" --out workflow.yaml
camflow batch <workflow.yaml> --inputs '<glob>' --out <dir>
camflow resume <run_dir> [--feedback "<text>"] [--steps N]
camflow run --from <node_id> --run-dir <run_dir> [--feedback "<text>"]
```

Planning is optional authoring assistance, never an implicit runtime step. The runtime does not create nodes, mutate workflows, automatically replan, manage mutable global state, route dynamically, loop, or execute internally in parallel.

## Migration rule

Existing source and operational documents describe the v1.1 implementation and are historical reference only. New implementation work starts from `design-v1.2.md`; incompatible v1.1 behavior must be removed or rejected rather than silently retained.


---

## Historical note

The prior v1.1 strategy text has been retired from this authority document because it defines incompatible stateful transitions, mutable state, and CAM-specific orchestration. The checked-in v1.1 implementation is documented in [`architecture.md`](architecture.md), [`self-monitoring.md`](self-monitoring.md), and Git history for migration reference.
