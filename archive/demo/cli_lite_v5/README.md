# CLI Lite v5

CLI Lite means the workflow can run **without CAM or any external daemon**.

This mode uses the shared `workflow.yaml` DSL, but Claude Code interprets the DSL itself.

## Core model

- `workflow.yaml` is the single workflow definition.
- `CLAUDE.md` defines how Claude should interpret and execute the DSL.
- `workflow-run` is a skill that runs the workflow.
- `healthy` is a skill that acts like a lightweight supervisor.
- `healthy` includes a deterministic script for checks and suggestions.
- Claude Code built-in `/loop` periodically invokes `/healthy`.
- A Stop hook prevents Claude from stopping before the workflow is done.

## Directory layout

```text
cli_lite_v5/
  workflow.yaml
  CLAUDE.md
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

## Start sequence

1. Open Claude Code in this directory.
2. Run `/workflow-run`.
3. Run `/loop 2m /healthy`.
4. Claude should keep running until the workflow reaches `done`.

## What `healthy` does

The `healthy` skill:

- reads current workflow state
- runs a deterministic check script
- detects repeated node / retry / missing progress patterns
- returns a status and suggestion
- helps Claude resume with a better strategy

## What this mode is good for

- lightweight setup
- no daemon dependency
- fast workflow experimentation
- portable project-local execution
