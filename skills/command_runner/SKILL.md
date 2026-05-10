---
name: command_runner
description: Run deterministic local commands or project scripts requested by a CamFlow node and emit a structured CamFlow envelope.
metadata:
  category: camflow-builtin
  tags: command, script, audit, test
disable-model-invocation: false
---

# Skill: command_runner

You run deterministic local commands or project scripts named in the node's
steps, then write `agent_output.json`.

Use this skill when a workflow needs a script-style audit or build step during
the normal node run phase. CamFlow nodes do not use `run.tool`; command
execution belongs inside a skill or inside `verify.command`.

## Rules

- Run only commands explicitly named by the node goal or steps.
- Prefer project-local scripts over inventing shell pipelines.
- Use common Linux commands and Python standard library. Do not assume optional
  host tools such as `jq`, `yq`, `qsub`, `smake`, `p4`, or custom CLIs unless
  the node explicitly established that they exist.
- If a referenced script already emits a CamFlow envelope, run it and normalize
  its result into this node's `agent_output.json`.
- If a command fails, return `status: "fail"` with `error.code:
  "COMMAND_FAILED"` and include the exit code plus recent stdout/stderr in
  `error.message` or `data.output`.

## Output Contract

Match the node's `output_schema` exactly. Common fields are:

- `passed` (boolean)
- `tests_run` (integer)
- `failed_tests` (array)
- `output` (string)
- `summary` (string)

When the command succeeds but the requested schema fields are not directly
available, fill them conservatively from exit status and captured output.
