# Transition Resolution Spec v0.1

Defines how runtime decides next step after node execution.

## Node output (normalized)

- status: success | fail | wait | abort
- summary: short text
- output: structured result
- memory_updates: dict
- control.action: continue | goto | wait | fail | abort
- control.target: optional node id

## Resolution priority

1. abort
2. wait
3. if fail (DSL)
4. ordered DSL conditions
5. control.goto
6. else
7. default (done / failed)

## Waiting semantics

- workflow.status = waiting
- runtime.pc = current node
- runtime.resume_pc = control.target or current

## Resume

- if waiting: pc = resume_pc
- status → running

## Principles

- node fail != workflow fail
- runtime owns workflow state
- DSL order defines priority
- trace is append-only and not used for decision
