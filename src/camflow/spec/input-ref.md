# Input Reference

Two reference forms in the DSL `with` field:

## Template variables: `{{...}}`

DSL value rendering. Substituted before execution.

- `{{state.error}}` → value of `state["error"]`
- `{{state.pc}}` → current node ID

## Context references: `@...`

File and namespace references. Resolved into attachments before executor call.

- `@memory.summary` → latest memory summary
- `@input.file` → input file reference
- `@artifact://path` → artifact reference

### Resolution priority

`@memory` → `@input` → `@output` → `@artifact` → file path

## Rules

- `with` may contain both forms
- `{{...}}` is for small inline substitution
- `@...` is for loading external context
- Do not use `@` for DSL logic
- Do not use `$` as core syntax
