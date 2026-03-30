# State Schema

## Runtime state

| Field | Type | Description |
|-------|------|-------------|
| `pc` | string | Program counter — current node ID. Initial: `"start"` |
| `status` | string | Workflow status: `running`, `done`, `failed`, `waiting`, `aborted` |
| `retry` | int | Current retry count for the active node. Reset on node change |
| `recovery_node` | string | Target node when retry budget is exhausted. Default: `"done"` |

## Update rules

- `state_updates` from node output are merged into state via `dict.update()`
- State is mutable — overwrites are allowed
- State is persisted after each step

## Init

Fresh workflow starts with:
```json
{"pc": "start", "status": "running"}
```
