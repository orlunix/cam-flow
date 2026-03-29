# CLI Light Project Template

This directory shows a Claude Code **CLI Light** setup where Claude owns the workflow loop and no external daemon drives each step.

## What this mode is

- Claude Code runs the workflow in-session.
- The built-in `/loop [interval] <prompt>` command is used to repeat a monitoring prompt while the session stays open.
- Project skills provide the workflow runner and health-check logic.
- A Stop hook can block session stop and tell Claude to continue.

## Files

- `CLAUDE.md` - project operating rules for CLI Light mode
- `.claude/skills/workflow-run/SKILL.md` - manual workflow execution skill
- `.claude/skills/healthy/SKILL.md` - health/monitor skill
- `.claude/settings.json` - project hook configuration
- `.claude/hooks/stop-guard.sh` - blocks stop when workflow is still running

## Suggested usage

1. Start Claude Code in this directory.
2. Run `/workflow-run` to start the workflow.
3. Run `/loop 2m /healthy` to re-check health every 2 minutes.
4. When the workflow completes, the Stop hook should allow Claude to stop.

## Notes

This mode is intentionally lighter than daemon-driven execution:

- easier to start
- less deterministic
- weaker recovery guarantees

Use daemon-driven CLI mode for stronger control.
