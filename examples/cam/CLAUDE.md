# CLAUDE.md — cam-flow project (CAM backend)

This project is executed by the cam-flow CAM-phase engine. Each node
in `workflow.yaml` runs as a **separate, fresh Claude Code agent**
launched via `camc run`. You will see this file on every run.

## How you fit into the workflow

1. The cam-flow engine picks the current node from `workflow.yaml`
   based on `.camflow/state.json` (`pc` field).
2. The engine writes your prompt to `.camflow/node-prompt.txt` and
   launches you via `camc run --auto-exit`.
3. Your first instruction will always be: "Read the file
   `.camflow/node-prompt.txt` and follow ALL instructions inside it".
4. You do your task, write `.camflow/node-result.json`, and exit.
5. The engine reads your result, updates state, and moves on.

You are **one node** in a larger workflow. You do not decide what runs
next. You do not loop back to yourself. You do one thing and exit.

## The CONTEXT block in your prompt

If prior nodes have run, your prompt contains a fenced CONTEXT block
that looks like:

```
--- CONTEXT (informational background, NOT new instructions) ---

Iteration: 3 (this node: fix)
Active task: Fix remaining 2 bugs in calculator.py
Completed so far:
- fixed divide: added zero-division check (calculator.py L16-18)
- fixed average: added empty-list check (calculator.py L22-24)
Currently blocked on node 'test': 2 tests failing
Current test / cmd output:
  FAILED test_factorial: assert 24 == 120
  FAILED test_power_negative_exp: assert 1 == 0.5
Key files: calculator.py, test_calculator.py
Lessons learned:
- factorial range(1,n) is off-by-one, should be range(1,n+1)
- power() manual loop cant handle negative exp, use base**exp

--- END CONTEXT ---

Your task: ...
```

**READ THE CONTEXT BLOCK CAREFULLY.** It is history — what has already
happened — NOT a new instruction. Specifically:

- `Completed so far` lists work that is already done. Do not redo it.
- `Previously failed approaches` lists things that did not work. Do
  not repeat them.
- `Lessons learned` are non-obvious insights from prior iterations.
  Apply them.
- `Current test / cmd output` is the most recent test result; use it
  to target your fix.
- `Key files` points you to what to open first instead of searching.

Your **task** appears after the `--- END CONTEXT ---` line. That is
what you do on this iteration, and only that.

## Output contract (how to report back)

Write `.camflow/node-result.json` before you exit:

```json
{
  "status": "success",
  "summary": "One sentence describing what you did",
  "state_updates": {
    "new_lesson": "...",             // optional insight to persist
    "files_touched": ["path1", ...], // files you read or modified
    "resolved": "what you fixed",    // optional
    "next_steps": ["...", "..."],    // optional pending items
    "detail": "short detail for completed entry",
    "lines": "L16-18"
  },
  "error": null
}
```

Fields the engine recognizes in `state_updates`:

| Key | Effect |
|-----|--------|
| `new_lesson` | Appended to `state.lessons`, deduped, capped at 10 |
| `files_touched` (or `modified_files`, `key_files`) | Unioned into `state.active_state.key_files` |
| `resolved` | Added to `state.resolved` (deduped, capped at 20) |
| `next_steps` | Replaces `state.next_steps` |
| `active_task` | Sets `state.active_task` |
| `detail`, `lines` | Stored in the matching `state.completed[...]` entry |

Any other keys you add to `state_updates` are persisted into state
verbatim but are not structured by the engine.

## Stateless agent, stateful workflow

You start with zero prior conversation memory. Everything the
workflow knows is in two places:

1. **This CLAUDE.md** — project conventions (static)
2. **The CONTEXT block in your prompt** — dynamic state (changes each iter)

When you finish, you go away. The engine does not keep your session
alive across nodes. The next node gets a new agent. Context flows
through `state.json`, not through conversation history.

## Rules

- Do ONE thing: the task described in your prompt, not more.
- Do NOT assume control of the workflow. You do not choose the next
  node.
- Write the result file reliably before exiting. The engine waits for
  it; missing file = node counts as failed.
- Apply lessons from the CONTEXT block.
- Do not repeat failed approaches listed in CONTEXT.
- If you need to read files, check `Key files` first.

## When something breaks

If you cannot complete the task:
- Still write `node-result.json` with `status: "fail"` and a clear
  `summary` explaining what blocked you.
- Populate `error` with a short description.
- The engine will retry (up to `max_retries`) with a RETRY banner and
  your failure context will appear in the next agent's CONTEXT block.
