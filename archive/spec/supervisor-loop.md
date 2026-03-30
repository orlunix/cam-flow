# Supervisor Loop Spec v0.1

## Purpose

The Supervisor Loop is an outer control loop that watches workflow execution health and performs deterministic repair actions when a workflow or agent becomes unhealthy.

It is **not** the business workflow itself.
It is **not** an LLM-driven reasoning loop.
It is a rule-based runtime supervision mechanism.

## Scope

The Supervisor Loop lives at the CAM side of the system.

### cam-flow owns

- workflow runtime state
- node execution
- transitions
- trace
- wait/resume semantics

### CAM Supervisor Loop owns

- health checks
- liveness checks
- stuck detection
- timeout detection
- fixed repair actions
- escalation to human
- supervisor event logging

## Design goals

- deterministic
- scriptable
- easy to debug
- safe first version
- no AI required in v0.1
- works with tmux/session-managed agents

## Non-goals for v0.1

- autonomous intelligent healing
- LLM-based diagnosis as default path
- complex policy engine
- distributed supervision

## Key idea

The Supervisor Loop periodically inspects workflow state and agent/session state.
When a rule matches an unhealthy condition, it applies a predefined repair action.

## Loop model

```text
collect snapshot -> evaluate rules -> execute repair action -> record supervisor event
```

## Inputs

A health snapshot may include:

- workflow_id
- workflow.status
- runtime.pc
- runtime.step
- runtime.resume_pc
- last trace timestamp
- last log timestamp
- current node runtime duration
- retry counters
- tmux session existence
- monitor existence
- agent process existence
- optional log pattern matches

## Health snapshot fields

Suggested minimal snapshot structure:

```json
{
  "workflow_id": "wf_001",
  "workflow_status": "running",
  "pc": "test",
  "step": 12,
  "resume_pc": null,
  "last_trace_ts": "2026-03-29T02:00:00Z",
  "last_log_ts": "2026-03-29T02:00:05Z",
  "node_elapsed_sec": 420,
  "session_exists": true,
  "monitor_exists": true,
  "agent_process_exists": true,
  "recent_error_pattern": null,
  "retry_count": 2
}
```

## Core rules for v0.1

### Rule 1: agent/session missing while workflow is running

Condition:

- workflow_status == `running`
- session_exists == false

Action:

- attempt basic recovery if possible
- if recovery fails, mark workflow failed
- create supervisor event

### Rule 2: no workflow progress for too long

Condition:

- workflow_status == `running`
- step unchanged for threshold seconds
- trace not advancing

Action:

- mark as stalled
- execute configured repair action

### Rule 3: current node timeout

Condition:

- workflow_status == `running`
- node_elapsed_sec > node timeout threshold

Action:

- interrupt or stop current agent activity
- optionally reroute to handoff/recovery node
- write supervisor event

### Rule 4: waiting too long

Condition:

- workflow_status == `waiting`
- waiting duration exceeds threshold

Action:

- remind, escalate, or mark for human review

### Rule 5: repeated failures exceeded budget

Condition:

- retry_count > configured max

Action:

- stop auto-healing
- escalate to human
- optionally move workflow into failed or waiting_human equivalent external handling

### Rule 6: matched known bad log pattern

Condition:

- recent_error_pattern matches configured pattern

Action:

- apply fixed repair command
- log supervisor action

## Repair action model

Repair actions should be fixed and deterministic.

Suggested initial action types:

- `send_text`
- `send_key`
- `interrupt`
- `stop_agent`
- `restart_monitor`
- `resume_workflow`
- `reroute_workflow`
- `fail_workflow`
- `escalate_human`
- `write_handoff`

## Repair action semantics

### send_text

Send a fixed text command to the agent/session.

Example:

- `continue`
- `please finish current step`
- a fixed recovery instruction

### send_key

Send a special key.

Example:

- Enter
- Ctrl-C
- Escape

### interrupt

Interrupt current agent execution, usually by Ctrl-C or equivalent transport action.

### stop_agent

Stop the managed agent/session.

### restart_monitor

Restart the CAM-side monitor process if it is unhealthy.

### resume_workflow

Resume a waiting or paused workflow.

### reroute_workflow

Move workflow `pc` to a fixed target node such as `handoff` or `recover`.

### fail_workflow

Move workflow to a failed state when automatic recovery should stop.

### escalate_human

Create a human-facing event or handoff packet for manual intervention.

### write_handoff

Force creation of a checkpoint/handoff artifact before a stronger action.

## Repair tiers

The repair system is easier to operate if actions are grouped by severity.

### Tier 1: gentle nudges

- send_text
- send_key

### Tier 2: controlled recovery

- interrupt
- write_handoff
- reroute_workflow
- resume_workflow

### Tier 3: hard stop / escalation

- stop_agent
- fail_workflow
- escalate_human

## Recommended first-version behavior

Start with a rule-based system only.

Do not use AI to decide health or repair actions in v0.1.
Use fixed commands, fixed thresholds, and fixed repair actions.

## Rule configuration

Rules should be configurable, not hardcoded forever.

Example structure:

```yaml
supervisor_rules:
  - name: stalled_node
    when:
      workflow_status: running
      no_progress_seconds: 600
    action:
      type: interrupt

  - name: agent_missing
    when:
      workflow_status: running
      session_missing: true
    action:
      type: fail_workflow
      reason: agent_session_lost

  - name: waiting_too_long
    when:
      workflow_status: waiting
      waiting_seconds: 3600
    action:
      type: escalate_human
```

v0.1 may implement this as Python dicts instead of YAML.

## Supervisor event log

The Supervisor Loop should maintain its own append-only event log, separate from workflow trace.

Suggested structure:

```json
{
  "ts": "2026-03-29T02:10:00Z",
  "workflow_id": "wf_001",
  "check": "stalled_node",
  "matched": true,
  "action": "interrupt",
  "detail": "no progress for 900 seconds"
}
```

## Why separate supervisor events from trace

- workflow trace records node execution history
- supervisor events record health decisions and external interventions

Without this separation it becomes hard to explain why an agent was interrupted or rerouted.

## Sampling interval

Suggested initial intervals:

- fast local dev: every 10 seconds
- normal operation: every 30 to 60 seconds

The interval should be configurable.

## Stuck detection guidance

Do not treat lack of output alone as stuck.

Use combined signals such as:

- no trace advance
- no step change
- no log update
- node elapsed time exceeded threshold
- session still exists

This reduces false positives for silent-but-healthy agent work.

## Integration points with CAM

Suggested CAM module:

```text
cam/core/supervisor_loop.py
```

Suggested functions:

- `collect_health_snapshot()`
- `evaluate_rules()`
- `execute_repair_action()`
- `record_supervisor_event()`

## Integration points with cam-flow

The Supervisor Loop may read:

- runtime.json or runtime store
- trace tail
- current workflow status
- last handoff ref

The Supervisor Loop may write or trigger:

- resume
- reroute
- fail
- handoff generation

## Safety principles

- repair actions must be deterministic
- dangerous actions should be tiered
- repeated auto-repair should have a budget
- after repeated failures, escalate rather than thrash

## Recommended first implementation order

1. health snapshot collection
2. basic rules
3. supervisor event log
4. simple actions: interrupt, fail, escalate
5. optional reroute/handoff integration

## Long-term extensions

Later versions may add:

- AI-assisted diagnosis
- richer policy engine
- per-node custom health logic
- distributed supervision
- adaptive thresholds

These are not required for v0.1.

## Summary

The Supervisor Loop is a CAM-side rule-based health and recovery loop.
It periodically checks workflow and agent state, detects unhealthy conditions, and applies fixed repair actions.
Its job is not to reason creatively, but to keep long-running workflows healthy, recoverable, and observable.
