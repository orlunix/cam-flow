# CLI Light v4

This is the corrected **CLI Light** setup for Claude Code.

## Key points

- `workflow.yaml` is the shared DSL.
- Claude Code reads and interprets the DSL itself.
- `CLAUDE.md` explains how to execute the DSL.
- Project skills provide `/workflow-run` and `/healthy`.
- Claude Code's built-in `/loop` command must be started manually in-session.
- A `Stop` hook checks `.claude/state/workflow.json` and blocks stop when the workflow is still running.

## Start sequence

1. Open Claude Code in this directory.
2. Run `/workflow-run`
3. Run `/loop 2m /healthy`

## Files

- `workflow.yaml`
- `CLAUDE.md`
- `.claude/skills/workflow-run/SKILL.md`
- `.claude/skills/healthy/SKILL.md`
- `.claude/settings.json`
- `.claude/state/workflow.json`

## Notes

- Loop is not started from `CLAUDE.md`.
- Loop is a built-in Claude Code slash command and must be launched in the interactive session.
- Skills create slash commands automatically.
