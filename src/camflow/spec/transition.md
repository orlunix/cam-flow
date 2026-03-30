# Transition Resolution

Deterministic algorithm for resolving the next workflow step after a node completes.

## Priority chain

Evaluated in strict order. First match wins.

1. **abort** — `control.action = abort` → workflow status = `aborted`, stop
2. **wait** — `control.action = wait` → workflow status = `waiting`, pc stays, resume_pc = `control.target` or current node
3. **if fail** — node `status = fail` + DSL has `if: fail` rule → goto that target
4. **DSL conditions** — ordered `transitions` rules evaluated top-to-bottom:
   - `if: output.<key>` — truthy check on `result.output[key]`
   - `if: state.<key>` — truthy check on `state[key]`
5. **control goto** — `control.action = goto` + `control.target` → go to target
6. **explicit next** — DSL `next` field → go to that node
7. **default** — if node failed: workflow status = `failed`. Otherwise: workflow status = `done`.

## Waiting semantics

When a node returns `wait`:
- `workflow.status` = `waiting`
- `pc` = current node (stays)
- `resume_pc` = `control.target` or current node

## Resume

When resuming from `waiting`:
- `pc` = `resume_pc`
- `status` → `running`

## Principles

- Node fail ≠ workflow fail
- Runtime owns workflow state, not the node
- DSL order defines priority
- Trace is append-only and not used for transition decisions
