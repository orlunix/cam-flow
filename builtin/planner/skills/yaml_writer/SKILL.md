# Skill: yaml_writer

Third (last) node of the builtin Planner workflow. You take the DAG
produced by `workflow_designer` and emit a syntactically valid
camflow `workflow.yaml` as a single string.

The upstream `workflow_designer` envelope is in your input under
`upstream.design_dag`. Use its `data.workflow_goal` (string), `data.dag`
(list of node dicts), and `data.context` (string).

## What you must produce

```json
{
  "yaml_text": "<the entire workflow.yaml file as a single string>"
}
```

## YAML format 

```yaml
workflow: <descriptive_snake_case_name>
version: "1.1"

goal: |
  <one-sentence concrete restatement of the user's objective — the
   value of upstream.design_dag.data.workflow_goal verbatim. This is
   the v1.1 Workflow.goal field; retry/review/final-audit judge against
   it. MUST be present and a non-empty string for any non-trivial
   workflow.>

context: |
  <multi-line text from design_dag's context;
   include the original user task somewhere here.>

nodes:
  - id: <id>
    goal: "<one-line intent>"
    steps:
      - "step 1"
      - "step 2"
    needs: [<upstream_id>, ...]    # optional
    run:
      skill: <skill_name>
    output_schema:                  # optional but recommended
      field: <type>
    verify:
      criterion: "<text>"          # OR command: "<bash>"  OR human: "<prompt>"
    retry: <int>                   # optional, default 1
```

## Rules

1. Output is `yaml_text`, **a string** — not a dict. The runtime parses
   it back into a dict before running it.
2. **Carry `workflow_goal` through to top-level `goal:`.** Take
   `upstream.design_dag.data.workflow_goal` verbatim and write it as
   the workflow's top-level `goal:` block. Don't skip it; don't
   paraphrase it. This is the persistent objective the retry/review/
   final-audit chain judges against.
3. **No `state:` or `inputs:` section.** they don't exist.
4. **No `run.input:` field.** Cross-node data flows through `needs`
   automatically.
5. **Match the spec exactly.** If `verify` is omitted, the runtime
   uses default agent verify with the steps as criterion. Don't write
   `verify: { agent: ... }` — that's not a thing.
6. **Quote strings that contain colons or special YAML chars.**
7. **`verify: human` is opt-in.** Carry it through ONLY if `design_dag`
   actually put it on a node, which it should only do when the user's
   original prompt asked for review/approval. Don't add `verify: human`
   on your own; don't drop it if it was intentionally placed.
8. **Only `run.skill` is valid.** If upstream design text mentions
   commands or scripts, keep them in the node's steps or
   `verify.command`, but write `run.skill`.

9. **Portable `verify.command`.** Carry through only command gates that
   use POSIX shell builtins, common Linux/coreutils commands, `python3`
   stdlib, or task-specific commands already known to exist. Do not
   assume optional host-tool dependencies such as `jq`, `qsub`, `smake`,
   `p4`, or custom CLIs. Use Python stdlib parsing for
   `agent_output.json` when possible. If design_dag hands you a
   missing/non-general command, rewrite it to a portable equivalent when
   obvious; otherwise keep the node as skill/agent verification rather
   than fabricating an unavailable shell dependency.

10. **`output_schema` types — strict allow-list of five names.** Every
   value in any node's `output_schema` map MUST be exactly one of:
   `string`, `integer`, `number`, `boolean`, `array`. If `design_dag`
   handed you anything else (`bool`, `int`, `float`, `list`,
   `array of <X>`, `string[]`, an inline object schema like
   `{id: string, ...}`, or `"object"` / `"any"`), normalize it before
   emitting:

   - `bool` → `boolean`
   - `int` → `integer`
   - `float` → `number`
   - `list` → `array`
   - `array of string`, `array of {...}`, `string[]` → `array`
     (element types are NOT part of v1.1 schema; document the shape
     in the node's goal/steps if you need to)
   - inline object schemas → either promote fields to top-level
     `output_schema` keys, or replace with `array` and let the goal
     describe per-element shape.
   - `"object"` / `"any"` / `"json"` / `null` → these are not v1.1
     types; pick the closest of the five (usually `string` for
     opaque blobs, `array` for collections).

   This is non-negotiable — workflow load fails on unknown type
   names, or worse, silently skips the type check and leaves the
   field unvalidated at runtime.

## On retry

Read `previous.feedback`. Typical complaints:

- "missing/wrong skill" → look at upstream `design_dag.data.dag` again
  and pick a different existing skill. Do not switch to `tool:`.
- "user wanted X but yaml does Y" → the user's complaint takes priority
  over the design_dag's choices; rewrite to match
- "yaml doesn't parse" → fix syntax (indentation, quoting, etc.)
