# CLI Light v2

This is a complete Claude Code **CLI Light** template.

## What this mode is

- Claude Code owns the main workflow loop.
- There is **no external daemon** driving every step.
- A project `CLAUDE.md` defines the workflow contract.
- Two project skills are used:
  - `/workflow-run` to execute the workflow
  - `/healthy` to check health / progress
- Claude Code's built-in `/loop` command periodically runs `/healthy`.
- A `Stop` hook prevents Claude from stopping while the workflow is still active.

## Directory layout

```text
cli_light_v2/
  CLAUDE.md
  .claude/
    settings.json
    state/
      workflow.json
    skills/
      workflow-run/SKILL.md
      healthy/SKILL.md
```

## How to use

1. Open Claude Code in this directory.
2. Run `/workflow-run`.
3. Run `/loop 2m /healthy`.
4. Claude will continue until the Stop hook allows exit.

## Important notes

- `CLAUDE.md` is the correct Claude Code project instruction file.
- Skills live in `.claude/skills/<skill-name>/SKILL.md`.
- `/loop` is a built-in Claude Code command.
- The `Stop` hook is configured in `.claude/settings.json`.
- This mode is lighter and less deterministic than daemon-driven CLI mode.
