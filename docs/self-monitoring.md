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
camflow run --daemon <workflow.yaml>     # background engine + watchdog
camflow run --daemon --no-watchdog ...   # background engine, no watchdog
camflow resume <workflow.yaml>           # pick up from last checkpoint
camflow resume <workflow.yaml> --from N  # jump pc to node N, then continue
camflow stop                             # SIGTERM the watchdog then engine
camflow stop --force                     # SIGKILL (no cleanup)
camflow status                           # engine + watchdog + workflow liveness
camflow status <workflow.yaml>           # override auto-discovery
camflow watchdog <workflow.yaml>         # run the watchdog loop manually
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

### Stale-lock recovery

On Linux local filesystems a process exit always releases flock, so
an abandoned lock file with a dead pid is only a paper tiger — the
kernel already cleared the real lock. But on NFS (especially when the
lockd has stale state), with unusual mount configurations, or after
edge-case `kill -9` scenarios, the kernel can keep flock blocked
while the pid recorded in the file points at nothing. To avoid making
the operator `rm .camflow/engine.lock` by hand, `EngineLock.acquire`
self-heals:

1. If `flock` blocks, read the pid from the file.
2. If the pid is **missing** (empty file) → respect the lock; another
   acquirer is mid-write.
3. If the pid is **alive** → respect the lock; a real engine is
   running.
4. If the pid is **dead**:
   * No heartbeat exists → stale. Unlink and retry once.
   * A heartbeat exists with a *live* pid → respect the lock (some
     other engine is alive; the lock file just has a drifted pid).
   * A heartbeat exists with a *dead* pid that is **fresh** (<5 min)
     → respect the lock. Gives a just-crashed engine a grace window.
   * A heartbeat exists with a *dead* pid that is **stale** (≥5 min)
     → stale. Unlink and retry once.

Unlinking a file while another process still holds flock on its
inode is safe: the stealer's `open` returns a brand-new inode and
the old flock becomes irrelevant.

## Watchdog process

The engine's heartbeat tells you *when* it's gone, but it can't bring
itself back. That's the watchdog's job.

When you start a daemonized engine (`camflow run --daemon` or
`camflow resume --daemon`), the parent forks a sibling — not a
child — process that runs `camflow watchdog` against the same
workflow. The watchdog and the engine share no state in memory; they
talk only through `.camflow/`.

```
camflow run --daemon workflow.yaml
    │
    ├── fork #1 — engine daemon (writes heartbeat.json, holds engine.lock)
    │
    └── fork #2 — watchdog daemon (holds watchdog.lock, writes watchdog.log)
```

### Decision loop

Every `--poll-interval` seconds (default 30) the watchdog reads
`state.json` and `heartbeat.json` and applies the rule below:

| state.status | heartbeat fresh? | engine pid alive? | watchdog action |
|---|---|---|---|
| `done` / `failed` / `interrupted` / `aborted` / `engine_error` | — | — | exit 0 (workflow is over) |
| `running` (or anything else) | yes | yes | sleep, poll again |
| `running` | yes | **no** | crash → `camflow resume --daemon` |
| `running` | **no** | yes | hung → `camflow resume --daemon` |
| `running` | **no** | no | crash → `camflow resume --daemon` |
| no `state.json` yet | — | — | sleep, poll again (engine still booting) |

The "stale heartbeat with a live pid" case is the most subtle: the
engine process is *there* but hasn't written a heartbeat in over 60
seconds (the watchdog uses a tighter threshold than the 120s
operator-facing one — see `DEFAULT_WATCHDOG_STALE_THRESHOLD`). That
indicates an internal hang — typically a `camc` command stuck on a
network call — and is treated identically to a crash. The new engine
spawned by `camflow resume` will hit the engine lock, find the dead
predecessor's pid, self-heal the lock, and continue.

### Restart budget

Default 3 auto-restarts (`--max-restarts 3` / `--watchdog-max-restarts`
on the spawn side). After the budget is exhausted the watchdog logs
`restart budget exhausted` and exits with code 2; manual intervention
is required. The counter is in-memory only — restarting the watchdog
itself resets it, which matches the intuition that the operator is
now involved and is expected to choose how aggressive to be.

A `--restart-cooldown` (default 45s) is applied after each
`camflow resume` spawn so the new engine has time to write its first
heartbeat before the watchdog's next poll, avoiding a false "engine
still dead" detection during the brief startup window.

### Files

| File                    | Written by | Read by | Lifetime |
|-------------------------|------------|---------|----------|
| `.camflow/watchdog.lock`| watchdog   | other watchdogs (acquire), `stop` (cleanup) | Removed on exit |
| `.camflow/watchdog.pid` | watchdog   | `status`, `stop` | Removed on exit |
| `.camflow/watchdog.log` | watchdog   | operators | Appended forever |

The lock uses the same `fcntl.flock` pattern as `EngineLock` and
auto-heals stale pids the same way: a re-spawn (e.g. when the
watchdog itself crashed without releasing) overwrites cleanly.

### Stop ordering

`camflow stop` always signals the **watchdog first**, waits for it to
exit, and only then signals the engine. Otherwise the watchdog would
observe the engine's death mid-shutdown and resurrect it from under
us. The engine's clean shutdown writes `status=interrupted` to
`state.json`, so even if the watchdog were still alive it would see
a terminal status and exit on its own — but stopping it first removes
the race entirely.

### Disabling

`camflow run --daemon --no-watchdog workflow.yaml` (and the same flag
on `camflow resume --daemon`) skips the watchdog spawn. Use it when
you want a one-shot daemon without auto-recovery (debugging,
experiments, embedding inside another supervisor like systemd).

### Manual invocation

```
camflow watchdog workflow.yaml \
    --poll-interval 30 \
    --max-restarts 3 \
    --stale-threshold 60 \
    --restart-cooldown 45
```

Useful for attaching a watchdog to an engine that was started
without `--daemon`, or for testing the watchdog in isolation.

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
Watchdog: ALIVE (pid 12346)
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
Watchdog: ALIVE (pid 12346)
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

### What `camflow stop` does NOT touch

`camflow stop` ends the **engine** (and the watchdog supervising it).
It does **not** touch the project's **Steward** — the Steward is
project-scoped and outlives any single flow on purpose, so you don't
lose project memory just because a flow ended.

To end the Steward explicitly, use `camflow steward kill` (or
`camc rm <steward-id>`). The order during a normal shutdown is:

```
camflow stop          → engine (and watchdog) exit cleanly
camflow steward kill  → only when you actually want to abandon the
                        project's persistent assistant
```

`camflow run --no-steward <yaml>` opts out of the Steward entirely;
in that mode `camflow stop` is the whole story.

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
| `watchdog.lock`   | watchdog          | competing watchdogs, `stop`   | Removed on exit |
| `watchdog.pid`    | watchdog          | `status`, `stop`              | Removed on exit |
| `watchdog.log`    | watchdog          | operators                     | Appended forever |
| `trace.log`       | engine main loop  | `evolve report`               | Appended forever |
| `progress.json`   | engine main loop  | humans, external dashboards   | Overwritten each step |
