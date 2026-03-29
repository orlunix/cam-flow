# Execution Backends Spec v0.1

## Purpose

Define how cam-flow supports multiple execution backends while keeping a single workflow language and a single runtime contract.

The system supports two execution styles:

- **Rule-driven backend**
- **SDK-driven backend**

These are two execution backends for the same workflow semantics.

They are not two different workflow systems.

---

## Core Principle

cam-flow must keep:

- one workflow DSL
- one runtime model
- one transition model
- one node output contract

Different backends may execute nodes differently, but they must not redefine workflow semantics.

---

## Why two backends exist

### Rule-driven backend

Useful for:

- quickly leveraging existing coding agents
- prompt/rule-driven execution
- fast experimentation
- human-guided workflows

### SDK-driven backend

Useful for:

- deterministic execution
- structured state handling
- trace, resume, and supervisor integration
- long-running industrial workflows

Both are valuable, but they solve different operational needs.

---

## 1. Rule-driven Backend

### Definition

A backend that uses an existing coding agent through its prompt/rule mechanism rather than a dedicated structured runtime API.

Examples of techniques:

- AGENT.md or RULES.md style guidance
- prompt-driven task execution
- natural-language handoff and repair commands
- adapter wrappers around existing agent CLIs

### Characteristics

- fast to adopt
- highly compatible with existing tools
- lower implementation effort
- more dependent on agent behavior
- less deterministic than SDK execution

### Best use cases

- early prototyping
- prompt and workflow validation
- rapid tool integration
- workflows with higher human involvement

---

## 2. SDK-driven Backend

### Definition

A backend that drives an agent through a structured SDK or programmatic execution interface.

Examples of techniques:

- SDK client invocation
- explicit options and structured inputs
- hooks integration
- deterministic parsing and normalization

### Characteristics

- higher implementation effort
- better control and observability
- better resume/retry/supervisor integration
- better fit for production execution

### Best use cases

- long-running workflows
- critical execution paths
- automated recovery
- structured integration with trace, memory, and handoff

---

## Shared Requirements

Both backends must obey the same cam-flow contracts.

### Shared workflow language

Both backends execute the same node definitions:

- `do`
- `if`
- `next`
- `set`
- `fail`
- `with`
- `@...`
- `{{...}}`

### Shared runtime model

Both backends operate under the same workflow runtime concepts:

- `pc`
- `status`
- `resume_pc`
- `memory`
- `trace`
- `handoff_ref`

### Shared node output contract

Both backends must normalize their results into the same structure.

Example:

```json
{
  "status": "success",
  "summary": "analysis complete",
  "output": {},
  "memory_updates": {},
  "artifact_refs": [],
  "handoff_ref": null,
  "control": {
    "action": "continue",
    "target": null,
    "reason": null
  },
  "error": null
}
```

This is mandatory.

---

## Architectural Model

```text
cam-flow DSL / Spec
        ↓
Execution Backend
   ├── Rule Backend
   └── SDK Backend
```

The workflow layer remains stable.
The backend layer is pluggable.

---

## Rule-driven Backend Responsibilities

A rule-driven backend is responsible for:

- building prompts and rule packages from workflow nodes
- injecting context from `with`, `@...`, and `{{...}}`
- calling an existing coding agent through CLI or equivalent interface
- capturing raw responses and logs
- normalizing the result into the cam-flow node output contract

### Important limitation

The rule-driven backend must not bypass:

- transition resolution
n- runtime state model
- memory policy
- supervisor policy

It is only an execution backend.

---

## SDK-driven Backend Responsibilities

An SDK-driven backend is responsible for:

- building structured execution inputs
- invoking agent SDKs or programmatic APIs
- integrating hooks where appropriate
- collecting structured outputs, logs, and events
- normalizing the result into the cam-flow node output contract

### Important limitation

The SDK-driven backend must not redefine the DSL or transition semantics.

---

## Recommended Operational Strategy

### Phase 1

Use the rule-driven backend first for:

- fast experimentation
- validating workflow language
- validating handoff patterns
- validating loop and supervisor behavior

### Phase 2

Use the SDK-driven backend to stabilize:

- deterministic execution
- resume and trace
- recovery and supervision
- production paths

### Phase 3

Keep both backends available:

- rule backend for compatibility and quick integration
- SDK backend for industrial-grade runtime paths

---

## Backend Selection Guidance

### Prefer rule-driven backend when:

- integrating with an existing coding agent quickly
- testing prompts and node patterns
- execution determinism is not yet the top priority

### Prefer SDK-driven backend when:

- execution must be stable and resumable
- recovery paths matter
- supervisor integration is important
- logs, trace, and state need stronger guarantees

---

## Anti-patterns

### Do not create two workflow languages

Wrong:

- one workflow syntax for rule mode
- another workflow syntax for SDK mode

Correct:

- one workflow syntax
- two execution backends

### Do not let the backend own workflow logic

The backend executes nodes.
The runtime owns workflow state and transitions.

### Do not let normalization drift

Both backends must produce the same node output shape.

---

## Suggested Repository Structure

```text
cam-flow/
  spec/
  engine/
  backends/
    rule_backend/
      README.md
      prompt_builder.py
      adapter.py
    sdk_backend/
      README.md
      executor.py
      adapter.py
```

---

## Summary

cam-flow supports two execution backends:

- a **rule-driven backend** for fast compatibility with existing coding agents
- an **SDK-driven backend** for deterministic, structured execution

They share one workflow language, one runtime model, and one node output contract.
This keeps the system flexible without splitting the architecture.
