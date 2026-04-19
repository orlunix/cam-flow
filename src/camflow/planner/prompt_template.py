"""Planner prompt template.

One prompt, one call, one generated workflow.yaml. Variable sections
(skills list, env info, project CLAUDE.md, few-shot examples) are
composed here from caller-provided inputs.
"""

from camflow.planner.examples import render_examples


PLANNING_RULES = """\
## Available node types

- `cmd <command>` — deterministic shell commands (builds, tests, linters,
  file checks). Prefer cmd wherever possible; the engine runs it as a
  subprocess, no LLM tokens spent.
- `agent claude` — AI agent for creative or analytical work. Every
  agent node runs as a FRESH camc-spawned Claude Code agent with
  stateless execution — the agent sees CLAUDE.md + the CONTEXT block
  you build via {{state.*}} refs.

## Planning rules

1. The FIRST node in the YAML is the entry node. It should analyze or
   understand before acting (e.g. run pytest first, then fix).
2. ONE concern per node. Never combine "fix and test" in a single
   agent — split them so the cmd test can gate the transition.
3. Every `agent` node MUST have a `verify` field: a shell command
   that objectively confirms the agent's claimed success. If verify
   exits non-zero the engine downgrades status=success → fail.
4. Every loop MUST have `max_retries` so it can't cycle forever.
   The engine also applies `EngineConfig.max_node_executions` (10 by
   default) as a hard loop-detection guard.
5. State keys are the handoff. Name them clearly
   (e.g. `state.cl_number`, not `state.x`) and document what each
   carries in the `with` field of the producing node.
6. The plan should be MINIMAL — fewest nodes to achieve the goal.
   More nodes means more retries means more chances to drift.
7. Agent nodes declare `methodology` (one of: rca, simplify-first,
   search-first, working-backwards, systematic-coverage).
8. Agent nodes declare `escalation_max` (0..4) — cap the escalation
   ladder at Ln. Non-critical nodes stay at 1 or 2; production
   fixes can go up to 4.
9. Agent nodes declare `allowed_tools` — only the tool subset the
   node actually needs. Fix nodes: [Read, Edit, Write, Bash].
   Analysis nodes: [Read, Glob, Grep, Bash]. Planning nodes:
   [Read, Write].

## verify-condition cookbook

- File created:   `verify: test -f <path>`
- File non-empty: `verify: test -s <path>`
- Build ok:       `verify: test -f <binary>`  (or re-run the build)
- Tests pass:     `verify: <same test cmd that proves success>`
- Search found:   `verify: test -n "{{state.result_key}}"`
- Code landed:    `verify: grep -q "<pattern>" <file>`

## Output contract

Generate ONLY a valid YAML workflow. No explanation before or after,
no ```yaml fences. Each node must have `do` and either `next` or
`transitions` (terminal nodes may omit both). Agent nodes MUST also
have `with`, `methodology`, `escalation_max`, `allowed_tools`,
`max_retries`, and `verify`.
"""


def build_planner_prompt(user_request, skills_list=None, env_info=None,
                          claude_md=None):
    """Compose the full planner prompt from caller-provided context.

    Args:
        user_request: natural-language task description (required).
        skills_list: list of (name, description) tuples or None.
        env_info: dict of {hostname, tools, paths, ...} or None.
        claude_md: string contents of the project's CLAUDE.md or None.

    Returns:
        A single prompt string ready to send to the LLM.
    """
    sections = [
        "You are the cam-flow workflow planner. Convert the user's request "
        "into a valid workflow.yaml for the cam-flow CAM backend.",
        "",
        PLANNING_RULES,
    ]

    if skills_list:
        sections.append("## Available skills")
        for name, desc in skills_list:
            sections.append(f"- `{name}`: {desc}")
        sections.append("")

    if env_info:
        sections.append("## Environment")
        for k, v in env_info.items():
            sections.append(f"- {k}: {v}")
        sections.append("")

    if claude_md:
        sections.append("## Project context (CLAUDE.md)")
        # Truncate enormous CLAUDE.md files so we don't blow the window.
        md = claude_md if len(claude_md) <= 4000 else claude_md[:4000] + "\n...[truncated]"
        sections.append(md)
        sections.append("")

    sections.append("## Few-shot examples")
    sections.append("")
    sections.append(render_examples())

    sections.append("## User request")
    sections.append(user_request)
    sections.append("")
    sections.append(
        "Generate the workflow.yaml now. Start with the YAML immediately "
        "— no preamble, no code fences."
    )

    return "\n".join(sections)
