# Webhook / Event Ingress Policy Spec v0.1

## Purpose

Define how external events enter the system and interact with workflows.

This spec focuses on **safe ingress and allowed actions**, not a full API platform.

## Scope

Covers:

- webhook/event input model
- allowed operations
- integration with workflow runtime and supervisor loop

Does NOT cover:

- full REST API design
- multi-tenant routing
- advanced retry/backoff systems

## Core Concept

Webhook is an **event ingress point**, not a direct executor.

It should not directly mutate workflow execution internals.

## Event Model

Suggested minimal structure:

```json
{
  "workflow_id": "wf_001",
  "event_type": "resume",
  "payload": {},
  "timestamp": "...",
  "token": "..."
}
```

## Allowed Actions

Webhook can trigger only controlled actions:

- resume workflow
- set approval flags
- enqueue external event
- notify supervisor

## Forbidden Actions

Webhook must NOT:

- directly modify trace
- inject node output
- arbitrarily change runtime state
- bypass transition rules

## Integration with Workflow

### Resume

Webhook can trigger resume:

- set runtime.status → running
- set runtime.pc → resume_pc

### External Signals

Webhook can set memory flags:

```json
{
  "memory.approved": true
}
```

## Integration with Supervisor Loop

Webhook can:

- notify supervisor loop
- inject events for rule evaluation

Example:

- human approval arrives
- supervisor loop detects event
- triggers resume

## Security Model

Minimum requirements:

- authentication token or signature
- source validation
- reject unauthenticated requests

## Design Principles

### Principle 1

Webhook is ingress, not execution.

### Principle 2

All actions must go through runtime or supervisor.

### Principle 3

No direct mutation of execution history.

### Principle 4

Keep interface minimal in v0.1.

## Example Flow

```text
External system -> Webhook -> Event -> Supervisor -> Runtime resume
```

## Future Extensions

- event bus integration
- retry policies
- richer event types
- multi-source ingestion

## Summary

Webhook provides a controlled, secure entry point for external signals.
It should trigger actions through the supervisor and runtime layers, not bypass them.
