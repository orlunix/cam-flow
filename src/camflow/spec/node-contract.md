# Node Output Contract

Every node execution must return a result matching this schema, regardless of which backend executes it.

## Required fields

| Field | Type | Values |
|-------|------|--------|
| `status` | string | `success`, `fail`, `wait`, `abort` |
| `summary` | string | Brief description of what happened |
| `output` | dict | Structured output data |
| `state_updates` | dict | Key-value pairs to merge into workflow state |
| `control` | dict | Flow control signals (see below) |
| `error` | dict or null | Error details if failed |

## Control object

| Field | Type | Values |
|-------|------|--------|
| `control.action` | string or null | `continue`, `goto`, `wait`, `fail`, `abort`, or `null` |
| `control.target` | string or null | Target node ID (for `goto` or `wait` resume point) |
| `control.reason` | string or null | Human-readable reason |

## Example

```json
{
  "status": "success",
  "summary": "Analysis complete, found root cause in auth module",
  "output": {"root_cause": "token expiry"},
  "state_updates": {"error": "token expiry in auth module"},
  "control": {"action": "continue", "target": null, "reason": null},
  "error": null
}
```

## Rules

- Node fail ≠ workflow fail. A failed node triggers transition resolution, not automatic workflow termination.
- `state_updates` and `output` must always be dicts (even if empty).
- `control.action = null` or `continue` means "proceed normally via DSL transitions".
