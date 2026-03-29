# Execution Modes Specification v0.1

cam-flow supports three execution modes.

## 1. CLI Light

Agent-native loop mode.

- The coding agent owns the loop.
- Workflow is expressed through rules, prompts, or AGENT.md style guidance.
- No external daemon is required.
- Lowest control, fastest iteration.

Best for:

- exploration
- prompt validation
- lightweight workflows

## 2. CLI

Daemon-driven loop mode.

- The daemon owns workflow state and transitions.
- The daemon selects the current node.
- The daemon compiles one node into one prompt.
- The agent executes exactly one node per call.
- The daemon parses the result and resolves the next step.

Best for:

- controlled execution
- traceable runs
- CAM integration
- supervisor-based recovery

## 3. SDK

Programmatic structured execution mode.

- Workflow logic remains daemon/runtime owned.
- Agent execution uses SDK/API calls rather than text-only CLI calls.
- Best fit for stronger contracts and future production paths.

Best for:

- structured integrations
- richer IO models
- stronger normalization guarantees

## Shared Rule

All three modes must share the same workflow semantics:

- same DSL concepts
- same state / trace / memory model
- same node result contract

Only the execution backend changes.
