# Self-Monitoring

CamFlow drives a workflow by orchestrating `camc` agents — and monitors
itself while doing so. Every running engine writes a heartbeat file,
holds an exclusive lock, and can be inspected or stopped from another
shell without touching the agents.

```
┌──────────────────────────────────────────────────────────────────┐
│                       camflow engine                             │
│                                                                  │
│   ┌─────────────┐          ┌───────────────────┐                 │
│   │ main loop   │  writes  │ HeartbeatThread   │ every 30s       │
│   │ (executes   │────────► │ .camflow/         │                 │
│   │  nodes via  │          │   heartbeat.json  │                 │
│   │  camc)      │          └───────────────────┘                 │
│   │             │                                                 │
│   │             │          ┌───────────────────┐                 │
│   │             │ flocks   │ EngineLock        │                 │
│   │             │────────► │ .camflow/         │                 │
│   │             │          │   engine.lock     │                 │
│   │             │          └───────────────────┘                 │
│   └──────┬──────┘                                                 │
│          │ spawns                                                 │
│          ▼                                                        │
│   ┌──────────────┐                                                │
│   │ camc agent   │  (actual LLM work happens here)                │
│   └──────────────┘                                                │
└──────────────────────────────────────────────────────────────────┘

         ▲                ▲                ▲
         │ reads          │ signals        │ reads
         │                │                │
    camflow status   camflow stop    camflow status
    (from any shell) (from any shell)(crash detection)
```

## CLI reference

```
camflow run <workflow.yaml>              # start from beginning (reset state)
camflow resume <workflow.yaml>           # pick up from last checkpoint
camflow resume <workflow.yaml> --from N  # jump pc to node N, then continue
camflow stop                             # SIGTERM the engine (graceful)
camflow stop --force                     # SIGKILL the engine
camflow status                           # engine + workflow liveness
camflow status <workflow.yaml>           # override auto-discovery
camflow plan "<request>"                 # generate a workflow from NL
camflow evolve report <dir>              # trace-based eval reports
camflow scout --type ... --query ...     # planner scout (read-only)
```

`run` versus `resume` is load-bearing: `run` wipes any prior
`state.json` + `heartbeat.json` in `.camflow/` (after acquiring the
lock, so no concurrent engine is harmed), whereas `resume` preserves
them and only flips status back to `running` if needed.

## Heartbeat mechanism

On engine start, a `HeartbeatThread` daemon begins writing
`.camflow/heartbeat.json` every 30 seconds:

```json
{
  "pid": 12345,
  "timestamp": "2026-04-21T12:00:00Z",
  "pc": "eval_swerv",
  "iteration": 3,
  "agent_id": "5130c656",
  "status": "running",
  "uptime_seconds": 1234,
  "workflow_path": "/abs/path/to/workflow.yaml",
  "started_at": 1745233000.0,
  "agent_started_at": 1745234000.0
}
```

Writes are atomic (`tmp + rename`), so a reader (`camflow status`,
`camflow stop`) never sees a half-written file. The file is removed
on clean shutdown; a stale file whose `pid` no longer exists means
the engine crashed.

### Staleness threshold

A heartbeat older than **120 seconds** is considered stale. With the
default 30 s write interval, that gives three missed writes before we
call the engine dead — tolerant of transient pauses (GC, heavy I/O)
while still detecting real crashes promptly.

## Engine lock

`EngineLock` is a `fcntl.flock`-based exclusive lock on
`.camflow/engine.lock`. Acquired before the main loop starts,
released on exit. A second engine attempting to start on the same
workflow hits `EngineLockError` with the holder's pid and exits
non-zero. This is what prevents two engines from racing to update
the same `state.json`.

Because flock is released when the file descriptor closes (including
on process crash), the lock never persists past an engine's death —
the next `camflow run` will succeed cleanly.

## Crash recovery flow

```
                camflow run workflow.yaml (again)
                            │
                            ▼
                   ┌─────────────────┐
                   │ acquire lock    │◄─── fails? another engine runs
                   │ (exclusive)     │     → print status hint, exit 1
                   └────────┬────────┘
                            │ acquired
                            ▼
        (reset path) wipe state.json + heartbeat.json
                            │
                            ▼
                   init state at first node
                            │
                            ▼
                   start HeartbeatThread → run loop
```

For `camflow resume` the flow skips the wipe and instead reads
`state.json` as-is:

* If status ∈ {`failed`, `aborted`, `engine_error`, `interrupted`} →
  flip to `running`, reset the current node's retry/execution
  budgets, clear the terminal error, keep `pc` where it was.
* If status == `running` but heartbeat is stale and the previous
  `pid` is gone → the engine crashed mid-node. Continue from the
  recorded `pc`; the orphan handler reaps any dangling agent.
* If status == `running` with a fresh heartbeat → lock acquisition
  will fail (another engine is live). `camflow status` will tell you
  who holds it.
* If status == `done` → require `--from <node>` to re-run explicitly.

## Inspecting state from another shell

```
$ camflow status
Workflow: /path/to/workflow.yaml
Engine:   ALIVE (pid 12345, heartbeat 5s ago)
Node:     eval_swerv (iteration 3, attempt 1)
Agent:    5130c656 (running, 2m 30s)
Progress: 1/4 nodes completed
  [done] eval_vexriscv
  [>>>>] eval_swerv
  [    ] eval_c910
  [    ] comparison
Uptime:   20m 34s
```

Dead:

```
$ camflow status
Workflow: /path/to/workflow.yaml
Engine:   DEAD (last heartbeat 12m ago, pid 12345 not running)
Node:     eval_swerv (iteration 3, attempt 1) — was in progress
Progress: 1/4 nodes completed
  [done] eval_vexriscv
  [XXXX] eval_swerv
  [    ] eval_c910
  [    ] comparison
Recovery: run `camflow resume /path/to/workflow.yaml` to continue from 'eval_swerv'
```

## Stopping an engine

```
$ camflow stop
Sending SIGTERM to engine pid 12345 (from heartbeat)...
Engine pid 12345 exited.
```

`camflow stop` reads the pid from `heartbeat.json` first, falling back
to `engine.pid` (only written by `--daemon` runs). It sends `SIGTERM`
by default — the engine's signal handler marks itself interrupted and
the main loop exits cleanly after the current node settles. If the
engine doesn't exit within `--timeout` seconds (default 10), the
command exits with code 1 and tells you to re-run with `--force`.

`--force` sends `SIGKILL` instead. Only use it for truly stuck
engines; SIGKILL means no cleanup, so any in-flight agent will be
detected as an orphan by the next `camflow resume`.

## Example session

```
# Start a fresh workflow.
$ camflow run examples/cam/workflow.yaml
camflow daemon started (pid 12345); logs at .camflow/engine.log

# Check progress from another shell.
$ camflow status
Workflow: /.../workflow.yaml
Engine:   ALIVE (pid 12345, heartbeat 4s ago)
Node:     build (iteration 1, attempt 1)
Progress: 0/2 nodes completed
  [>>>>] build
  [    ] verify

# Kill it politely.
$ camflow stop
Sending SIGTERM to engine pid 12345 (from heartbeat)...
Engine pid 12345 exited.

# Pick up where it left off.
$ camflow resume examples/cam/workflow.yaml
[resume] applied:
  - status 'interrupted' → 'running'
  - reset retry_counts['build'] to 0
...
Workflow finished: status=done, pc=verify
```

## Files in `.camflow/`

| File              | Written by        | Read by                       | Lifetime |
|-------------------|-------------------|-------------------------------|----------|
| `state.json`      | engine main loop  | engine, `status`, `resume`    | Persisted across runs |
| `heartbeat.json`  | `HeartbeatThread` | `status`, `stop`, recovery    | Removed on clean exit |
| `engine.lock`     | `EngineLock`      | competing engines             | Removed on exit |
| `engine.pid`      | `--daemon` only   | `stop` (fallback)             | Persisted; stale after exit |
| `engine.log`      | `--daemon` only   | operators                     | Appended forever |
| `trace.log`       | engine main loop  | `evolve report`               | Appended forever |
| `progress.json`   | engine main loop  | humans, external dashboards   | Overwritten each step |
