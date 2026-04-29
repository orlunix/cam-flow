---
name: orchestrator
description: Exception handler + human-in-the-loop driver for a workflow run. Watches for halt events, triages, and either auto-resumes with feedback or escalates to a human operator. v0.9 — runtime not yet wired; this file is the role spec only.
role: orchestrator
invocation: top_level
tools: Read, Bash
---

# Agent: orchestrator (v0.9 — placeholder)

**Status: not yet wired into runtime.** This file documents the role; the
runtime won't spawn it until v0.9. Build dependencies on it carefully.

## Role

The Orchestrator handles the lifecycle events the runner can't decide on
its own:

- **Halt events** — when a node returns `status: halted` or retry
  exhausts. The orchestrator decides: auto-resume with new state /
  re-plan / escalate to human.
- **Human-in-loop** — when a workflow needs approval, choice, or
  external information that the runner alone can't provide.
- **Cross-run coordination** — sometimes a sweep / parallel runs need
  joint decisions; orchestrator owns that.

## Invocation

The orchestrator drives the runner via the **`camflow` CLI** (not by
importing the runtime). It reads `<run_dir>/halt.json`, inspects the
trace, decides an action, and issues `camflow resume <run_dir>` (or
`stop` / a follow-up `plan`) accordingly.

This memory entry is load-bearing:
[orchestrator_drives_via_cli.md](../memory/orchestrator_drives_via_cli.md)

## v0.9 implementation TODO (not yet done)

- runtime hook: emit `orchestrator_signal` events and a sidecar
  `orchestrator-request.json` when halts occur, instead of just halting
- camflow CLI: `camflow inspect <run_dir>` for read-only state inspection
  (subset of `status` + `trace` + halt.json)
- `camflow resume <run_dir> --feedback <str>` already exists; extend with
  `--state-patch <json>` so orchestrator can patch state before resume
- this AGENT.md: write the actual decision-making prompt

For now: `camflow stop` / `camflow resume` are user-driven (a human is
the orchestrator). v0.9 swaps the human for an LLM agent.
