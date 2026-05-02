# Skill: yaml_writer

Third (last) node of the builtin Planner workflow. You take the DAG
produced by `workflow_designer` and emit a syntactically valid
camflow v1.1 `workflow.yaml` as a single string.

The upstream `workflow_designer` envelope is in your input under
`upstream.design_dag`. Use its `data.dag` (list of node dicts) and
`data.context` (string).

## What you must produce

```json
{
  "yaml_text": "<the entire workflow.yaml file as a single string>"
}
```

## YAML format (camflow v1.1)

```yaml
workflow: <descriptive_snake_case_name>
version: "1.1"

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
      skill: <skill_name>          # OR tool: <path>
    output_schema:                  # optional but recommended
      field: <type>
    verify:
      criterion: "<text>"          # OR command: "<bash>"  OR human: "<prompt>"
    retry: <int>                   # optional, default 1
```

## Rules

1. Output is `yaml_text`, **a string** — not a dict. The runtime parses
   it back into a dict before running it.
2. **No `state:` or `inputs:` section.** v1.1 doesn't have them.
3. **No `run.input:` field.** Cross-node data flows through `needs`
   automatically.
4. **Match v1.1 spec exactly.** If `verify` is omitted, the runtime
   uses default agent verify with the steps as criterion. Don't write
   `verify: { agent: ... }` — that's not a thing.
5. **Quote strings that contain colons or special YAML chars.**
6. **`verify: human` is opt-in.** Carry it through ONLY if `design_dag`
   actually put it on a node, which it should only do when the user's
   original prompt asked for review/approval. Don't add `verify: human`
   on your own; don't drop it if it was intentionally placed.
7. **`run.tool:` is rare.** Carry it through ONLY if `design_dag`
   placed it (which means design_dag judged the §10 5-criterion bar is
   met). Don't change a `skill` to a `tool`. Don't change a `tool` to a
   `skill`. The decision belongs to design_dag, not yaml_writer.

## On retry

Read `previous.feedback`. Typical complaints:

- "missing/wrong skill" → look at upstream `design_dag.data.dag` again,
  pick a different skill, or change to `tool:`
- "user wanted X but yaml does Y" → the user's complaint takes priority
  over the design_dag's choices; rewrite to match
- "yaml doesn't parse" → fix syntax (indentation, quoting, etc.)
