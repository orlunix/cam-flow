# Input Reference Spec v0.1

## Purpose

Define how cam-flow references variables, files, and external materials inside workflow definitions.

This spec intentionally separates:

- **logical variables** used by the DSL
- **context references** used by prompts and executor inputs

## Design Principle

cam-flow uses two different reference forms:

- `{{ ... }}` for **DSL/template variables**
- `@...` for **context/input references**

They are not interchangeable.

---

## 1. Template Variables: `{{ ... }}`

Template variables are used for structured substitution inside DSL fields such as `set`, `with`, and other templated values.

### Examples

```text
{{input.file}}
{{memory.root_cause}}
{{output.error_summary}}
```

### Intended usage

- DSL value rendering
- memory updates
- small structured substitutions
- condition-related values

### Rules

- Must resolve to a scalar or a small structured value
- Must not be used as a file loader mechanism
- Should remain deterministic and explicit

---

## 2. Context References: `@...`

`@` is used to reference external input material that should be provided to an executor.

### Supported forms in v0.1

#### A. File reference

```text
@docs/spec.md
@configs/build.yaml
@logs/test.log
```

Default interpretation: local file path.

#### B. Runtime namespaces

```text
@memory.root_cause
@input.file
@output.error_summary
```

These expand to runtime values for prompt/context use.

#### C. Artifact reference

```text
@artifact://wf_001/handoff_12
```

These refer to previously created artifacts or handoff/checkpoint objects.

---

## 3. Namespace Resolution Rules

Resolution priority in v0.1:

1. `@memory.` → working memory value
2. `@input.` → workflow input value
3. `@output.` → last node output value
4. `@artifact://...` → artifact reference
5. otherwise → treat as file path

### Examples

```text
@memory.root_cause   -> runtime memory lookup
@docs/spec.md        -> file path
@artifact://...      -> artifact lookup
```

---

## 4. Intended Semantic Difference

### `{{ ... }}`

Means:

> substitute a logical/template value into DSL or text

### `@...`

Means:

> attach or expand an external input reference for executor consumption

This distinction is important.

---

## 5. Usage in `with`

`with` may contain both forms.

### Example

```yaml
with: |
  Fix this issue using:

  Root cause:
  @memory.root_cause

  Spec:
  @docs/spec.md

  Build config:
  @configs/build.yaml

  Summary:
  {{memory.handoff_brief}}
```

Interpretation:

- `@memory.root_cause` is a context reference
- `@docs/spec.md` is a file reference
- `{{memory.handoff_brief}}` is a template substitution

---

## 6. Runtime Expansion Model

Before calling an executor, runtime should scan `with` and resolve `@...` references.

Suggested internal normalized form:

```json
{
  "prompt": "Fix this issue using root cause ...",
  "attachments": [
    {
      "kind": "file",
      "path": "docs/spec.md",
      "content": "..."
    },
    {
      "kind": "artifact_ref",
      "ref": "artifact://wf_001/handoff_12"
    }
  ]
}
```

---

## 7. File Handling Policy

### Default behavior

- small files may be embedded
- large files should be passed by reference or rejected depending on runtime policy

### Recommended default types

- markdown
- text
- config
- yaml
- json

### Error behavior

If a referenced file or namespace value does not exist:

- v0.1 recommended behavior: fail the node before executor call

---

## 8. Anti-patterns

### Do not use `@` for DSL logic

Bad:

```text
if @memory.retry > 3
```

Use:

```text
if memory.retry > 3
```

### Do not use `$` as the core DSL variable syntax

`$VAR` is shell-like and not expressive enough for hierarchical workflow state.

### Do not use `@` as a replacement for all templating

Use the right tool for the right layer.

---

## 9. Summary

cam-flow uses:

- `{{ ... }}` for logical/template substitution
- `@...` for file, artifact, and runtime context references

This keeps the DSL compact while preserving a clean separation between:

- workflow logic
- executor context input
