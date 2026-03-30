# Recovery Policy

## Actions

| Action | Description |
|--------|-------------|
| `retry` | Re-execute current node (if retry budget allows) |
| `reroute` | Jump to `recovery_node` (default: `done`) |

## Decision logic

1. If `retry < 2` → action = `retry`, target = current node
2. If `retry >= 2` → action = `reroute`, target = `state.recovery_node` or `"done"`

## Future extensions (not in v0.1)

- Escalate to human
- Write handoff artifact before reroute
- Configurable recovery strategies per node
