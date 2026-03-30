# CLI Lite v7

CLI Lite v7 aligns with the **formal cam-flow DSL** used by the CLI and SDK modes.

## Goals

- use the same DSL fields as the main spec:
  - `do`
  - `with`
  - `next`
  - `transitions`
- keep Lite mode standalone (no CAM or external daemon required)
- make state and memory handling explicit
- make workflow-run and healthy prompts strong enough to parse and execute the DSL reliably

## Core model

- `workflow.yaml` is the shared workflow definition
- `CLAUDE.md` defines Lite runtime rules
- `workflow-run` is the main skill that parses and executes the DSL
- `healthy` is the supervisor-like skill that checks progress, loop patterns, and suggestion quality
- `.claude/state/workflow.json` stores execution state and lightweight memory
- `.claude/state/memory.json` stores semantic memory summaries and lessons
- Claude Code built-in `/loop` periodically invokes `/healthy`
- a Stop hook blocks session stop until the workflow reaches `done`

## Start sequence

1. Open Claude Code in this directory.
2. Run `/workflow-run`.
3. Run `/loop 2m /healthy`.
4. Claude should continue until the workflow reaches `done`.

## Directory layout

```text
cli_lite_v7/
  workflow.yaml
  CLAUDE.md
  reference/
    workflow/
      dsl.md
      state-schema.md
      memory-schema.md
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
      memory.json
    hooks/
      stop_guard.sh
    settings.json
```

## Design rules

- skill-local `template.md` is the runtime execution template Claude actually uses
- `reference/` contains fuller documentation and examples for humans
- `workflow-run` must parse formal DSL fields, not a Lite-only shortcut
- `healthy` must treat state change as proof of execution progress
