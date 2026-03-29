# Memory Policy Spec v0.1

## Purpose

Define how runtime memory is structured, written, and constrained in cam-flow.

This spec focuses on **boundaries and write rules**, not advanced memory systems.

## Scope

This applies to:

- runtime memory
- node output memory updates
- DSL `set` behavior
- interaction with trace and artifacts

It does NOT cover:

- long-term knowledge memory
- vector memory
- AI-based memory compression

## Memory Layers

### 1. Input

Immutable or minimally modified data passed at workflow start.

Examples:

- task description
- file paths
- initial parameters

### 2. Working Memory

Small, mutable key-value store used for decisions.

Examples:

- retry counters
- flags
- root cause
- status markers

### 3. Handoff References

Pointers to checkpoint artifacts.

Examples:

- last_handoff_ref
- summary references

## What Memory Is NOT

- not a log store
- not a raw output store
- not a full history store

## Write Sources

Memory can be updated from:

### 1. Node output (`memory_updates`)

```json
{
  "memory_updates": {
    "retry": 2,
    "last_error": "reset failed"
  }
}
```

### 2. DSL `set`

```yaml
set:
  memory.retry: +1
  memory.root_cause: "{{output.root_cause}}"
```

### 3. Runtime internal updates

- last_output summary
- last_handoff_ref

## Write Rules

### Rule 1: small and essential only

Memory must only contain small, decision-relevant values.

### Rule 2: no large payloads

Large text, logs, or raw outputs must go to artifacts/logs.

### Rule 3: overwrite allowed

Memory is mutable; latest state replaces old values.

### Rule 4: deterministic writes

Memory updates must be explicit and traceable.

## Memory vs Trace vs Artifact

| Type     | Purpose                | Size     | Mutability |
|----------|----------------------|----------|------------|
| memory   | current state         | small    | mutable    |
| trace    | execution history     | medium   | append-only|
| artifact | raw outputs/logs      | large    | immutable  |

## Handoff Interaction

- full handoff stored as artifact
- memory stores only reference or brief summary

Example:

```json
{
  "last_handoff_ref": "artifact://wf_001/handoff_12",
  "handoff_brief": "root cause reset path"
}
```

## Size Guidelines

- memory values should be short strings, numbers, or small dicts
- avoid nested large structures

## Anti-patterns

- storing full logs in memory
- storing full LLM responses in memory
- using memory as trace

## Summary

Memory is a small, structured, mutable state used for workflow decisions.
It should remain compact, explicit, and separate from logs and artifacts.
