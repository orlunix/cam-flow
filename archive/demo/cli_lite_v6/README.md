# CLI Lite v6

CLI Lite means a workflow can run **without CAM or any external daemon**.

This mode still uses the shared `workflow.yaml` DSL, but Claude Code interprets the DSL itself.

## Core model

- `workflow.yaml` is the single workflow definition.
- `CLAUDE.md` defines how Claude should interpret and execute the DSL.
- `workflow-run` is a skill that runs the workflow from the DSL.
- `healthy` is a skill that acts like a lightweight supervisor.
- `healthy` includes a deterministic script for checks and suggestions.
- Claude Code built-in `/loop` periodically invokes `/healthy`.
- A Stop hook prevents Claude from stopping before the workflow is done.

## Start sequence

1. Open Claude Code in this directory.
2. Run `/workflow-run`.
3. Run `/loop 2m /healthy`.
4. Claude should keep running until the workflow reaches `done`.

## Structure

```text
cli_lite_v6/
  workflow.yaml
  CLAUDE.md
  reference/
    workflow/
      dsl.md
      state-schema.md
    skills/
      workflow-run/
        template.md
        examples.md
      healthy/
        template.md
        examples.md
        strategy.md
  .claude/
    skills/
      workflow-run/
        SKILL.md
        template.md
      healthy/
        SKILL.md
        template.md
        scripts/
          healthy_check.py
    state/
      workflow.json
    hooks/
      stop_guard.sh
    settings.json
```

## Design rule

- Skill-local `template.md` is the execution template Claude actually uses.
- `reference/` contains the fuller human-readable versions and examples.

## Notes

This mode is useful for:
- lightweight setup
- portable project-local execution
- fast workflow experimentation

Use daemon-driven CLI mode for stronger control and stronger recovery.
