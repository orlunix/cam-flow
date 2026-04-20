"""Planner prompt template.

One prompt, one call, one generated workflow.yaml. Variable sections
(skills list, agents catalog, env info, project CLAUDE.md, few-shot
examples, optional domain rule pack, optional scout reports) are
composed here from caller-provided inputs.
"""

import json

from camflow.planner.examples import render_examples


# Hard cap so a misconfigured caller can't blow the prompt window.
MAX_SCOUT_REPORTS = 3


PLANNING_RULES = """\
## Available node types (DSL v2)

- `shell <command>` — deterministic shell commands (builds, tests,
  linters, file checks). Prefer `shell` wherever possible; the engine
  runs it as a subprocess, no LLM tokens spent. `cmd` is still accepted
  as a legacy alias for `shell` but new plans should use `shell`.
- `agent <name>` — named sub-agent from `~/.claude/agents/<name>.md`.
  Use this whenever a matching agent exists in the Agent catalog
  below; the agent carries its own persona, allowed tools, and
  pre-loaded skills. Prefer a named agent over `agent claude` whenever
  one matches the job.
- `agent claude` — legacy anonymous agent. Accepted for back-compat.
- `skill <name>` — invoke an installed skill by name; runs inside an
  agent session.
- Inline prompt — any `do` value that doesn't start with a keyword
  above is treated as a free-text prompt to the default agent. Use
  for tiny, one-off tasks that don't merit a named agent.

## Planning rules

1. The FIRST node in the YAML is the entry node. It should analyze or
   understand before acting (e.g. run pytest first, then fix).
2. ONE concern per node — **one node = one independently verifiable
   deliverable**. If a node would need to do X then check then do Y,
   split it into separate nodes. Never combine "fix and test" in a
   single agent — split them so the shell test can gate the transition.
3. Every `agent` / `skill` / inline node MUST have a `verify` field
   that checks the **OUTCOME, not the OUTPUT**. A file existing does
   not prove the goal was met; a binary compiling does not prove the
   simulation ran; a log containing "done" does not prove the test
   actually executed. Verify the thing you actually care about.
4. **Preflight.** Any node whose body is expensive (timeout > 5 min,
   or any simulation / build / fetch / install) MUST declare a
   `preflight:` shell cmd that checks prerequisites in seconds. The
   engine runs preflight BEFORE the body; on non-zero exit the body is
   skipped and the node fails immediately as PREFLIGHT_FAIL. Preflight
   checks prerequisites ("can we start?"), verify checks the outcome
   ("did we succeed?").
5. Every loop MUST have `max_retries` so it can't cycle forever.
   The engine also applies `EngineConfig.max_node_executions` (10 by
   default) as a hard loop-detection guard.
6. State keys are the handoff. Name them clearly
   (e.g. `state.cl_number`, not `state.x`) and document what each
   carries in the `with` field of the producing node.
7. The plan should be MINIMAL — fewest nodes to achieve the goal.
   More nodes means more retries means more chances to drift.
8. Agent nodes declare `methodology`. Choose based on what the node DOES:
   - `simplify-first` — environment setup, build, deploy, write report.
     Operations that should be as simple as possible. Question assumptions,
     remove unnecessary complexity, just get it done.
   - `search-first` — research, code analysis, finding changelists, reading
     RTL. The node needs to FIND and UNDERSTAND information before acting.
   - `rca` — debugging, fixing test failures, diagnosing errors.
     Reproduce the issue, isolate the component, form hypotheses, verify.
   - `working-backwards` — design, planning, creating verification plans.
     Define the desired outcome FIRST, then design backwards to reach it.
   - `systematic-coverage` — running tests, executing verification, code review.
     Enumerate all cases, prioritize edge cases, prove correctness.
9. Agent nodes declare `escalation_max` (0..4) — cap the escalation
   ladder at Ln. Non-critical nodes stay at 1 or 2; production
   fixes can go up to 4.
10. Agent nodes declare `allowed_tools` — only the tool subset the
    node actually needs. Fix nodes: [Read, Edit, Write, Bash].
    Analysis nodes: [Read, Glob, Grep, Bash]. Planning nodes:
    [Read, Write].
11. Deterministic operations are `shell`, not `agent`. If a step is
    always the same command (smake, vcs, pytest, jg, npm run build)
    it does not need an AI. Wrapping it in an agent just burns tokens
    and adds failure modes.

## verify-condition cookbook (OUTCOME, not OUTPUT)

- File created:       `verify: test -f <path>`
- File non-empty:     `verify: test -s <path>`          (often better)
- Build actually ran: `verify: test -f <binary> && <binary> --version`
- Tests pass:         `verify: <same test cmd that proves success>`
- Search found:       `verify: test -n "{{state.result_key}}"`
- Code landed:        `verify: grep -q "<pattern>" <file>`
- Simulation ran:     `verify: test -s trace.mon.log`  (not just simv exists)
- Proof actually new: `verify: grep -q "<this-run-marker>" REPORT.md`

## preflight cookbook

- Tree built:         `preflight: test -x <simv>`
- Core boots:         `preflight: timeout 60 <simv> +quick_test > /dev/null 2>&1`
- Service alive:      `preflight: curl -sf http://localhost:8080/health`
- Memory width ok:    `preflight: test "$(wc -c < <hex>)" -gt 1024`
- Staging green:      `preflight: ./scripts/smoke-staging.sh`

## Scouts (read-only context probes)

You may receive a "Scout reports" section below containing the output
of two scout types the camflow-manager ran on your behalf BEFORE this
prompt:

- **skill-scout** — `skillm search <capability>` plus the first lines
  of each matching SKILL.md. Use this to pick `skill <name>` nodes
  instead of guessing. If a scout report contains no candidates for a
  capability you need, do NOT invent a skill name — fall back to
  `agent <name>` (from the Agent catalog) or an inline prompt.
- **env-scout** — `which <tool>` + version probes. Use this to confirm
  a `shell <tool>` node will actually run. If a tool is "available:
  false" in the scout report, plan around it (use a different tool, or
  add a `preflight:` to fail fast with a clear message).

Scouts are READ-ONLY and already ran — you cannot request more from
inside this prompt. If the scout reports are missing a capability /
tool you need, plan defensively (preflight checks, fallback nodes).

## Output contract

Generate ONLY a valid YAML workflow. No explanation before or after,
no ```yaml fences. Each node must have `do` and either `next` or
`transitions` (terminal nodes may omit both). Agent / skill / inline
nodes MUST also have `with` (unless the inline prompt IS the task),
`methodology`, `escalation_max`, `allowed_tools`, `max_retries`, and
`verify`. Expensive nodes MUST also declare `preflight`.
"""


# ------------- domain-specific rule packs (DSL v2 §38) --------------
#
# Each pack augments PLANNING_RULES with domain conventions. Selected
# via `domain=` in build_planner_prompt. Unknown domain → no pack.

DOMAIN_PACKS = {
    "hardware": """\
## Domain rules: hardware / RTL verification

- Before writing a testbench or running a benchmark, add an
  **analyze-DUT** node that reads the RTL interface, memory widths,
  reset sequence, and clock domains. The planner cannot know these
  a priori — the agent must discover them from the code.
- Before running a benchmark or test, add a **validate-DUT-alive**
  node (tiny +quick_test or reset+poll) so we don't burn an hour on
  a sim whose core never booted.
- Check memory / bus widths before selecting hex or .mem files.
  Mismatch is silent and fatal.
- Deterministic tools are shell, not agent:
  `smake`, `vcs`, `jg`, `formal.tcl`, `waveform-gen`.
- Long simulations (> 5 min) MUST have preflight (core-alive check)
  and verify (actual trace/log content, not just "sim finished").
""",
    "software": """\
## Domain rules: software / testing

- Run ONE unit test or a small smoke subset before the full suite.
  Full suites hide which specific change regressed.
- Install / pin dependencies before invoking the code under test.
- Deterministic tools are shell, not agent:
  `pytest`, `npm`, `cargo`, `go test`, lint / format commands.
- Fix nodes: allowed_tools = [Read, Edit, Write, Bash].
  Analysis nodes: allowed_tools = [Read, Glob, Grep, Bash].
- Verify the test itself was executed, not just that pytest exited 0
  (e.g. `grep -q 'passed' pytest.log` AND `grep -q '<target-test-name>' pytest.log`).
""",
    "deployment": """\
## Domain rules: deployment / rollout

- Stage before production — the plan must include a staging or
  canary node that gates the production rollout.
- Preflight every production step with a health-check shell cmd.
- Verify the deployment actually took effect (hit the endpoint and
  check the version, don't just trust the CI log).
- Rollback path: every production deploy node should have an
  `if: fail` transition to a rollback node.
""",
    "research": """\
## Domain rules: research / investigation

- search-first methodology for the exploration phase; the agent must
  enumerate sources before drawing conclusions.
- Verify the finding came from THIS run, not from historical data the
  agent stumbled onto (grep for a this-run marker in REPORT.md).
- Final node is a human-readable report written to REPORT.md.
""",
}


def _render_scout_reports(scout_reports):
    """Render up to MAX_SCOUT_REPORTS scout dicts into a prompt section.

    Two shapes are recognized — both produced by `planner.scouts`:

      skill-scout:
        {"query": str, "tool": str, "candidates": [{name, description,
         summary, path}], "warnings": [str]}
      env-scout:
        {"checks": [str], "results": {tool: {available, path, version,
         ...}}, "warnings": [str]}

    Anything else falls through to a JSON dump of the report so the
    Planner at least sees the raw data.
    """
    if not scout_reports:
        return ""
    capped = list(scout_reports)[:MAX_SCOUT_REPORTS]

    out = ["## Scout reports (read-only, already ran)"]
    for i, report in enumerate(capped, 1):
        if not isinstance(report, dict):
            out.append(f"### Scout {i}: (malformed)")
            continue

        if "candidates" in report:  # skill-scout shape
            query = report.get("query", "?")
            tool = report.get("tool", "unknown")
            candidates = report.get("candidates") or []
            out.append(f"### Scout {i}: skill-scout (tool={tool})")
            out.append(f"Query: {query}")
            if not candidates:
                out.append("Candidates: (none matched)")
            else:
                out.append(f"Candidates ({len(candidates)}):")
                for c in candidates:
                    name = c.get("name", "?")
                    desc = (c.get("description") or "").strip()
                    if len(desc) > 200:
                        desc = desc[:200] + "..."
                    out.append(f"- `skill {name}` — {desc}")
            for w in (report.get("warnings") or [])[:5]:
                out.append(f"  warning: {w}")

        elif "results" in report and isinstance(report["results"], dict):
            # env-scout shape
            results = report["results"]
            out.append(f"### Scout {i}: env-scout")
            for spec, info in results.items():
                kind = info.get("kind") if isinstance(info, dict) else "?"
                avail = info.get("available") if isinstance(info, dict) else False
                if kind == "tool":
                    path = info.get("path") or "—"
                    version = info.get("version") or "—"
                    line = (
                        f"- `{spec}`: available={avail}  "
                        f"path={path}  version={version}"
                    )
                elif kind == "path":
                    line = (
                        f"- `{spec}`: available={avail}  "
                        f"type={info.get('type', '?')}"
                    )
                else:
                    line = f"- `{spec}`: {json.dumps(info, sort_keys=True)}"
                out.append(line)
            for w in (report.get("warnings") or [])[:5]:
                out.append(f"  warning: {w}")
        else:
            out.append(f"### Scout {i}: (raw)")
            out.append(json.dumps(report, sort_keys=True)[:1500])
    return "\n".join(out)


def build_planner_prompt(user_request, skills_list=None, env_info=None,
                         claude_md=None, agents_list=None, domain=None,
                         scout_reports=None):
    """Compose the full planner prompt from caller-provided context.

    Args:
        user_request: natural-language task description (required).
        skills_list: list of (name, description) tuples or None.
        env_info: dict of {hostname, tools, paths, ...} or None.
        claude_md: string contents of the project's CLAUDE.md or None.
        agents_list: list of dicts (from agent_loader.list_available_agents)
            describing named sub-agents available via `do: agent <name>`.
        domain: optional domain rule-pack key — one of "hardware",
            "software", "deployment", "research". Unknown values are
            silently ignored.
        scout_reports: optional list of dicts (from `planner.scouts.run_*_scout`
            or `camflow scout` JSON output). Hard-capped at MAX_SCOUT_REPORTS
            entries; rendered into the prompt as a "Scout reports" block so
            the Planner LLM can choose skills/agents/preflights informed by
            actual environment + skill catalog probes (Option B).

    Returns:
        A single prompt string ready to send to the LLM.
    """
    sections = [
        "You are the cam-flow workflow planner. Convert the user's request "
        "into a valid workflow.yaml for the cam-flow CAM backend.",
        "",
        PLANNING_RULES,
    ]

    if domain and domain in DOMAIN_PACKS:
        sections.append(DOMAIN_PACKS[domain])
        sections.append("")

    if agents_list:
        sections.append("## Agent catalog (from ~/.claude/agents/)")
        for a in agents_list:
            name = a.get("name", "?")
            desc = a.get("description") or "(no description)"
            tools = a.get("tools") or []
            skills = a.get("skills") or []
            line = f"- `agent {name}`: {desc}"
            if tools:
                line += f"  [tools: {', '.join(tools)}]"
            if skills:
                line += f"  [skills: {', '.join(skills)}]"
            sections.append(line)
        sections.append("")

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

    rendered_scout = _render_scout_reports(scout_reports)
    if rendered_scout:
        sections.append(rendered_scout)
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
