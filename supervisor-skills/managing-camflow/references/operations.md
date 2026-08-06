# Camflow supervision and recovery

Use this guide while launching, observing, and recovering one run.

## Use short identities

Choose a top-level `workflow` name that remains meaningful in 12 characters,
such as `rvdbg`. Choose semantic node IDs that remain meaningful in 18
characters, such as `test_or_dut`, `lsu_debug`, `ifu_debug`, `goal_audit`, and
`update_memory`.

Camflow creates every worker and verifier with this shape:

```text
name: cf-<flow-label>-<node-label>-<8hex>
tags: cf-<flow-label>, cf-<flow-id>
```

Read the exact identity from `run.json`:

```json
{
  "flow": {
    "id": "1a2b3c4d",
    "name": "rvdbg",
    "label": "rvdbg",
    "tags": ["cf-rvdbg", "cf-1a2b3c4d"]
  }
}
```

Use the readable tag to see all runs of one workflow and the ID tag to isolate
one run:

```bash
camc list --tag cf-rvdbg
camc list --tag cf-1a2b3c4d
```

If the supervisor itself is launched through camc, give it a similarly short
human name such as `cf-rvdbg-sup-<short>` and the readable flow tag when the
caller controls creation. Do not let the supervisor impersonate a node.

## Trigger safely

Keep these directories separate:

```text
.camflow/plan/rvdbg/       editable authoring files
packages/rvdbg/            reviewed reusable bundle
cases/bug_001.json         immutable real case input
runs/rvdbg/bug_001-a/      one fresh run snapshot and outputs
```

Require a new empty run directory. Start one Camflow command and retain its
exit result. Do not background it without a durable process/session and a way
to keep observing it.

The runner snapshots workflow, input, and local skills before execution. It
records workflow and input hashes in `run.json`; resume and run-from reject
mutated snapshots.

## Observe durable state

Use the following evidence in order:

1. Process exit code and `result: done|halted` output.
2. `run.json` for workflow/input hashes and exact flow identity.
3. `trace.jsonl` for node starts, completions, retries, skips, routes, resume,
   run-from, halt, and completion.
4. `halt.json` for the terminal halt cause.
5. `nodes/<node>/attempt-N/input.json`, `prompt.txt`, `agent_output.json`,
   `verify.json`, and `output.json` for one attempt.
6. `agent.id`, `agent.json`, `camc-lifecycle.json`, and `camc-archive/*.tar.gz`
   for agent identity and cleanup evidence.
7. `nodes/<branch>/skip.json` for a non-selected route.

Treat an attempt as active when `node_started` exists without a matching
`node_completed`/`node_failed`, its camc agent is still present under the exact
flow tag, and the configured agent timeout has not expired.

Poll at a measured interval appropriate to the task, normally 15-30 seconds
for active debugging agents. Inspect more frequently only near a timeout or
after a state transition. Never modify attempt files while Camflow is active.

## Diagnose a halt

Classify the cause before choosing an action:

| Evidence | Meaning | Normal action |
| --- | --- | --- |
| `request_human` | Worker needs missing information or a decision | Ask one concrete question, then `resume --feedback` |
| `retry_exhausted` with actionable verifier feedback | Task and graph remain valid | Correct environment if needed, then resume once with precise feedback |
| `step_limit` | Supervisor/test budget paused execution | Resume to continue with the next pending work |
| `unmatched_route` | Router emitted an unsupported value or branch coverage is incomplete | Run-from the router if its output was wrong; create a revised workflow if the vocabulary/branches were wrong |
| `deadlock` | Graph completion cannot progress | Fix the authoring graph and start a new run |
| Workflow/input hash error | A run snapshot was modified | Do not bypass the check; use the original snapshot or start a new run |
| `CAMC_ARCHIVE_FAILED` | Session evidence was not durably archived | Keep the camc record, repair/perform archive, then clean up and resume |
| `CAMC_CLEANUP_FAILED` | Archive exists but the live record was not removed | Preserve archive, inspect lifecycle, clean the leftover record, then decide whether rerunning is safe |
| Repeated identical failure | Feedback is not changing capability or evidence | Stop retrying; revise the node/skill or escalate |

Do not remove a camc record after archive failure until the session is safely
preserved. The intended successful order is:

```text
archive -> status snapshot -> stop -> rm
```

## Choose resume, run-from, or a new run

Use `resume` when all are true:

- The workflow topology, node contract, and immutable input are correct.
- The failed node is still the correct next unit of work.
- New feedback, information, or repaired infrastructure can change the result.
- Repeating that node is safe.

Resume passes explicit feedback into the next attempt and leaves completed
upstream state intact.

Use `run --from NODE` when all are true:

- A completed node's result itself must be recomputed.
- Its downstream results are now invalid.
- The snapshotted workflow and input remain correct.
- A checkpoint copy of the pre-rerun artifacts exists if they must remain
  auditable.

Run-from removes the selected node directory and every downstream node
directory before recomputing them. Do not use it as a casual retry command.

Start a new run from a revised authoring workflow when any are true:

- A node is too broad, too narrow, or assigned the wrong skill.
- An edge, route value, schema, verifier, or memory policy must change.
- The real input must change.
- A side effect makes replaying the existing node unsafe.

Never edit `runs/<run>/workflow.yaml` or `input.json` to force a recovery.

## Maintain ownership to completion

After each recovery command, return to monitoring. Do not consider a resumed
command itself success. Require new trace events and a terminal result.

Stop and request human direction when:

- A node explicitly requests a human.
- Required data or access cannot be obtained safely.
- An external memory write is not authorized.
- Two supervisor recovery attempts have not produced materially new evidence.
- Fixing the problem would expand the workflow's objective or side effects.

On successful completion, verify:

- The last terminal event is `workflow_completed` with success.
- All required nodes have successful `output.json` or an expected `skip.json`.
- The selected route is visible in trace and branch artifacts.
- The goal audit contains concrete evidence.
- Memory references are recorded for agent nodes.
- Memory writeback is either recorded with stable entry IDs or retained as an
  explicit proposal.
- Camc lifecycle records show durable archive and completed cleanup, or any
  exceptional live record is clearly reported.
