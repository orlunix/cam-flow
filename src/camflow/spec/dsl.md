# DSL Schema

cam-flow workflow is defined in YAML. Each top-level key is a node ID.

## Node fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `do` | string | yes | Executor type: `skill <name>`, `cmd <command>`, or `agent <name>` |
| `with` | string | no | Task prompt. Supports `{{state.x}}` template variables and `@ref` context references |
| `next` | string | no | Explicit next node ID |
| `transitions` | list | no | Conditional routing rules: `[{if: condition, goto: target}]` |
| `set` | dict | no | State/memory updates to apply after execution |

## Executor types

- `skill` — invoke a named skill
- `cmd` — run a shell command
- `agent` — delegate to a sub-agent

## Transition rules

Each transition rule has:
- `if`: condition string — `success`, `fail`, `output.<key>`, `state.<key>`
- `goto`: target node ID

## Workflow constraints

- Must have a `start` node
- All `next` and `goto` targets must reference existing nodes
- Unknown fields on a node are invalid

## Example

```yaml
start:
  do: skill analyze
  with: |
    Analyze error: {{state.error}}
  next: fix

fix:
  do: skill fix
  with: |
    Propose a fix for: {{state.error}}
  next: test

test:
  do: skill test
  with: |
    Validate the fix
  transitions:
    - if: success
      goto: done
    - if: fail
      goto: fix

done:
  do: skill summarize
  with: |
    Summarize the final solution
```
