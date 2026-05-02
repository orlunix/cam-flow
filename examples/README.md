# Examples

In v1.1, `workflow.yaml` is a **compiler output** of the Planner — not
something users author. So this directory is intentionally minimal:

- **`bug-fix-compiled/`** — a reference snapshot of what Planner might
  produce for a typical bug-fix prompt. Treat this as a worked example
  of v1.1's YAML shape (`context`, no `inputs:`, no `run.input`,
  `nodes` with auto-injected `needs`).

To run a workflow in v1.1, give camflow a prompt:

```
camflow run "Fix the TypeError on line 87 of foo.py"
```

The Planner builtin (`camflow/builtin/planner/`) compiles your prompt
into a `workflow.yaml`, which the runtime then executes.

The v1.0 hand-authored examples are preserved in
`examples-v1.0-archive/` for historical reference; they use the cut
`state:` / `--state` / `{{state.X}}` / `run.input` mechanisms and will
not run on v1.1 unmodified.
