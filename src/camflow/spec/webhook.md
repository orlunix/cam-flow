# Webhook Policy

Webhook is an event ingress point, NOT a direct executor.

## Event structure

| Field | Type | Description |
|-------|------|-------------|
| `workflow_id` | string | Target workflow |
| `event_type` | string | `resume`, `approve`, `external_event`, `notify` |
| `payload` | dict | Event-specific data |
| `timestamp` | float | Unix timestamp |
| `token` | string | Authentication token |

## Allowed actions

- `resume_workflow` — resume from waiting state
- `set_memory_flag` — set a flag in working memory
- `enqueue_event` — queue event for supervisor
- `notify_supervisor` — alert supervisor loop

## Forbidden

- Directly modify trace
- Inject node output
- Bypass transition rules
- Arbitrarily change runtime state

## Security

- All requests must have authentication token
- Source validation required
- Reject unauthenticated requests
