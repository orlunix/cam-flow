# cam-flow Specifications

This directory is organized around the current daemon-driven execution model.

## Recommended reading order

### 1. Core model

- `core/model.md`
- `core/state.md`
- `core/trace.md`
- `core/memory.md`

### 2. Execution model

- `execution/daemon.md`
- `execution/transition.md`
- `execution/node.md`

### 3. Runtime control

- `runtime/supervisor.md`
- `runtime/hooks.md`

### 4. Backends

- `backends/rule-backend.md`
- `backends/sdk-backend.md`

### 5. Input model

- `input-reference.md`

## Current primary architecture

The current primary direction is:

- daemon-driven execution
- external state / trace ownership
- agent executes one node at a time
- rule backend for Claude CLI compatibility
- SDK backend for future direct integration

## Notes on older files

Some earlier files remain in the repository for historical context.

For the current architecture, prefer the files listed above.
