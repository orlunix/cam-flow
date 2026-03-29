# Rule Backend Spec v0.1

## Purpose

Define how the rule-driven execution backend runs cam-flow nodes by using an existing coding agent through prompts, rules, and deterministic normalization.

This backend is designed for fast integration with existing coding agents while preserving cam-flow runtime semantics.

## Goal

The rule backend should let a coding agent execute cam-flow nodes as consistently as possible without giving the agent control over workflow transitions.

The runtime still owns:

- workflow state
- transition resolution
- memory updates
- trace
- supervisor interaction

The agent only executes the current node and returns a normalized node result.

## Core Principle

The agent does **not** interpret the whole workflow.
It only executes a **single node task** at a time.

This is the main stability rule.

Wrong model:

- send the entire workflow DSL to the agent
- ask the agent to decide control flow for the whole system

Correct model:

- runtime picks one node
- backend translates that node into a bounded execution prompt
- agent performs that step only
- backend normalizes the output into the node result contract
- runtime resolves transitions

## Execution Model

```text
workflow runtime -> select current node -> build bounded node prompt
-> invoke coding agent -> capture output -> normalize output
-> return NodeResult -> runtime resolves next step
```

## What the agent should know

For each node execution, the rule backend should provide:

- current node id
- node type (`skill`, `cmd`, or `agent`)
- current task prompt (`with` after reference expansion)
- relevant runtime context
- strict output requirements
- explicit prohibition against controlling workflow state directly

## What the agent should not own

The agent must not be asked to:

- decide final workflow status
- mutate trace directly
- choose arbitrary next nodes outside allowed outputs
- reinterpret the DSL globally

## Stability Strategy

To maximize stability, the rule backend should use the following strategy.

### 1. Single-node execution only

Each invocation should be scoped to one node.

### 2. Explicit role prompt

The agent should be told that it is acting as a node executor, not as the workflow controller.

### 3. Strict response schema

The agent must be asked to produce a structured result that can be normalized into the cam-flow NodeResult shape.

### 4. Bounded context

Only the minimum required context should be passed in.
Do not dump the whole workflow unless strictly necessary.

### 5. Deterministic post-processing

The backend should parse and validate the agent output before handing it to the runtime.

### 6. Fall back to failure on malformed output

If the result cannot be normalized safely, return a failed NodeResult rather than guessing.

## Recommended prompt shape

Each node execution prompt should contain these sections.

### A. Execution role

Example:

```text
You are executing exactly one cam-flow node.
You are not the workflow controller.
You must only complete the current node task and return a structured result.
```

### B. Current node definition summary

Example:

```text
Node ID: analyze
Executor Type: agent
```

### C. Current task input

This includes expanded `with` text and resolved references.

### D. Output schema instructions

Require the agent to emit a machine-parsable block.

### E. Constraints

Example:

```text
Do not decide workflow transitions.
Do not invent state changes outside the allowed result fields.
If unsure, return a failure with a clear reason.
```

## Suggested normalized output target

The rule backend should try to normalize the agent result into this structure:

```json
{
  "status": "success",
  "summary": "one-line summary",
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

## Recommended agent return format

A practical rule-backend prompt should ask the agent to end with a JSON object.

Example:

```json
{
  "status": "success",
  "summary": "root cause identified",
  "output": {
    "root_cause": "missing reset assignment",
    "need_human": false
  },
  "memory_updates": {
    "root_cause": "missing reset assignment"
  },
  "control": {
    "action": "continue",
    "target": null,
    "reason": null
  },
  "error": null
}
```

The backend should still validate this before accepting it.

## Prompt template guidance

The rule backend should construct prompts from reusable templates instead of ad hoc strings.

Suggested template parts:

- role header
- node summary
- context section
- allowed result schema
- final JSON requirement

## Integration with `@` and `{{}}`

Before building the prompt:

- `{{...}}` should be rendered as template substitutions
- `@...` should be resolved as input/context references

The agent should receive the resolved text and referenced materials, not raw unresolved syntax unless debugging explicitly requires it.

## Failure handling

If the coding agent:

- returns malformed output
- times out
- stalls
- ignores schema

The rule backend must return a failed NodeResult with a structured error.

Example:

```json
{
  "status": "fail",
  "summary": "rule backend could not normalize agent output",
  "output": {},
  "memory_updates": {},
  "artifact_refs": [],
  "handoff_ref": null,
  "control": {
    "action": "fail",
    "target": null,
    "reason": "malformed output"
  },
  "error": {
    "code": "RULE_BACKEND_PARSE_ERROR",
    "message": "agent did not return a valid structured result",
    "retryable": true
  }
}
```

## Recommended first-version implementation

The first version should support:

- prompt construction from one node
- optional agent rule file generation
- raw response capture
- JSON extraction from response
- NodeResult normalization
- explicit parse failure handling

It should not yet try to make the coding agent understand the entire workflow language.

## Summary

The rule backend should treat the coding agent as a bounded node executor.
The runtime remains the workflow controller.
This is the key to making a rule-driven backend stable enough to be useful.
