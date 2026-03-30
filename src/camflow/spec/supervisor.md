# Supervisor Loop

Rule-based outer control loop watching execution health. NOT business logic, NOT LLM-driven.

## Ownership

- **CAM owns**: health checks, liveness, stuck detection, timeout, fixed repair actions, escalation
- **cam-flow owns**: workflow state, node execution, transitions, trace, wait/resume

## Loop model

1. Collect health snapshot
2. Evaluate rules
3. Execute repair action (if triggered)
4. Record supervisor event

## Repair actions (by tier)

### Tier 1: Gentle nudges
- `send_text` — prompt the agent
- `send_key` — keystroke injection

### Tier 2: Controlled recovery
- `interrupt` — interrupt current execution
- `restart_monitor` — restart monitoring
- `resume_workflow` — resume from wait
- `reroute_workflow` — jump to recovery node

### Tier 3: Hard stop
- `stop_agent` — terminate agent session
- `fail_workflow` — mark workflow as failed
- `escalate_human` — alert human operator

## v0.1 scope

- Rule-based only, NO AI for health decisions
- Fixed commands, thresholds, and actions
- Sampling interval: 10s (dev), 30-60s (production)
