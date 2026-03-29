# CLI Light v3 (DSL-Driven)

This demo shows the **CLI Light** mode with a **shared workflow DSL**.

## Core idea

- `workflow.yaml` is the single workflow definition.
- Claude Code reads and interprets the DSL itself.
- `.claude/state/workflow.json` stores current progress.
- `/workflow-run` drives execution.
- `/healthy` checks for stalls, loops, and missing progress.
- Claude Code's built-in `/loop` command periodically calls `/healthy`.

## Files

- `workflow.yaml` - shared workflow DSL
- `CLAUDE.md` - CLI Light execution instructions
- `.claude/skills/workflow-run/SKILL.md` - run the workflow from DSL
- `.claude/skills/healthy/SKILL.md` - monitor current DSL execution
- `.claude/settings.json` - Stop hook example
- `.claude/state/workflow.json` - workflow state template

## Suggested usage

1. Open Claude Code in this directory.
2. Run `/workflow-run`.
3. Run `/loop 2m /healthy`.
4. Claude should keep executing the workflow until completion.

## Important note

This mode is still agent-led. It is useful for fast experimentation, but it is less deterministic than daemon-driven CLI mode.
