可以。这里应该把接口收敛成 **plan / pack / run** 三个动作：

```text
plan = generate editable artifacts
pack = clean/copy artifacts into reusable bundle
run  = execute bundle/workflow with real input
```

下面是一版可以 append 到 `docs/design-v1.2.md` 的 spec section。

## Appendix: Simple plan / pack / run interface

### Design goal

CamFlow v1.2 keeps the runtime core small and stable, but users should not be forced to hand-write every YAML, JSON, and skill file.

The user-facing workflow is:

```text
plan -> user edits artifacts -> pack -> run
```

Each step has a narrow responsibility:

```text
plan = generate editable artifacts
pack = clean/copy artifacts into a reusable bundle
run  = execute workflow.yaml with real input.json
```

There is no package manager in v1.2.
There is no install step.
There is no registry.
There is no package replay system.
There is no package-specific runtime.

A package is just a clean directory.

---

### 1. `camflow plan`

`plan` is an authoring helper.

It should generate real editable artifacts whenever enough information is available.

Example:

```bash
camflow plan "debug this RISC-V core hang: case_id=bug_001 test=rv_rand_001 seed=12345 sim_log=/runs/bug_001/sim.log trace_log=/runs/bug_001/trace.log" \
  --out .camflow/plan/core_hang
```

Expected output:

```text
.camflow/plan/core_hang/
  workflow.yaml
  input.json
  input.template.json
  skills/
    retire_extractor/SKILL.md
    ifu_debugger/SKILL.md
    lsu_debugger/SKILL.md
    verdict_writer/SKILL.md
  validators/
    check_evidence.py
  README.md
  plan_manifest.json
```

`workflow.yaml` is the generated workflow.

`input.json` is a real runnable input file if the user supplied enough case information.

`input.template.json` records the expected input shape and can be used to create future case inputs.

`skills/` contains generated skill skeletons or generated skill instructions.

`validators/` may contain optional deterministic checkers used by `verify.command`.

`plan_manifest.json` records what was generated and whether the plan is runnable.

If required input information is missing, `plan` must not silently create a fake runnable `input.json`.

In interactive mode, `plan` may ask follow-up questions.

In non-interactive mode, `plan` must fail with a clear error.

Example failure:

```text
ERROR: cannot generate real input.json.
Missing required fields:
  - case_id
  - sim_log
  - trace_log
```

Draft placeholders are allowed only in `input.template.json`, not in real `input.json`.

---

### 2. User edits after plan

After `plan`, users are expected to inspect and edit the generated artifacts.

Editing can happen in any tool:

```text
Cursor
Claude Code
Codex
manual editor
other agents
```

The editable artifacts are ordinary files:

```text
workflow.yaml
input.json
input.template.json
skills/*/SKILL.md
validators/*
README.md
```

CamFlow does not own the editing loop.

CamFlow only requires that the final files are valid when passed to `run`.

---

### 3. `camflow pack`

`pack` creates a clean reusable bundle directory.

It is intentionally simple.

There is only one required pack command:

```bash
camflow pack SOURCE_DIR --out packages/core_hang
```

`SOURCE_DIR` may be a plan output directory or a previous run directory.

Examples:

```bash
camflow pack .camflow/plan/core_hang --out packages/core_hang
```

```bash
camflow pack runs/bug_001 --out packages/core_hang
```

`pack` copies only reusable authoring artifacts.

A packed bundle should look like:

```text
packages/core_hang/
  workflow.yaml
  input.template.json
  skills/
    retire_extractor/SKILL.md
    ifu_debugger/SKILL.md
    lsu_debugger/SKILL.md
    verdict_writer/SKILL.md
  validators/
    check_evidence.py
  README.md
  package_manifest.json
```

`pack` should exclude run outputs and temporary artifacts.

Forbidden package contents include:

```text
.camflow/
runs/
output/
outdir/
nodes/
attempt-*/
trace.jsonl
halt.json
run.json
input.json
evidence.json
symptoms.json
hypotheses.json
verdict.json
report.md
venv/
build/
logs/
*.log
*.fsdb
*.vcd
*.vpd
```

`input.json` is a per-run input and must not be included in a reusable package.

`input.template.json` may be included.

`package_manifest.json` should be simple metadata, not a lockfile:

```json
{
  "package_schema": "simple-v1",
  "name": "core_hang",
  "created_by": "camflow pack",
  "entry": "workflow.yaml",
  "input_template": "input.template.json",
  "skills_dir": "skills"
}
```

`pack` should fail fast if required reusable files are missing:

```text
ERROR: missing workflow.yaml
ERROR: missing input.template.json
ERROR: missing skill file: skills/lsu_debugger/SKILL.md
```

There is no separate `pack validate` command in v1.2.

Validation happens naturally when `pack` runs and again when `run` executes.

---

### 4. `camflow run`

`run` is the stable execution interface.

It executes a workflow with a real input.

From a packed bundle:

```bash
camflow run packages/core_hang/workflow.yaml --input cases/bug_001.json --out runs/bug_001
```

From a plan directory:

```bash
camflow run .camflow/plan/core_hang/workflow.yaml --input .camflow/plan/core_hang/input.json --out runs/bug_001
```

The runtime must be strict.

It should fail if any required artifact is missing or malformed:

```text
workflow.yaml not found      -> ERROR
workflow.yaml invalid YAML   -> ERROR
input.json required missing  -> ERROR
input.json invalid JSON      -> ERROR
input_schema mismatch        -> ERROR
skill missing                -> ERROR
SKILL.md missing             -> ERROR
unknown workflow key         -> ERROR
next/goto/routes present      -> ERROR
invalid restricted when      -> ERROR
```

`run` must not silently call `plan`.

`run` must not silently call `pack`.

`run` must not generate missing skills.

`run` must not generate missing input.

`run` must not fall back to unrelated host skills.

`run` executes exactly what was provided.

---

### 5. Batch

Batch uses the same workflow over many real inputs.

```bash
camflow batch packages/core_hang/workflow.yaml \
  --inputs "cases/*.json" \
  --out runs/core_hang_batch
```

Each input file produces one run directory.

The package directory remains read-only and reusable.

---

### 6. Directory ownership

The package directory contains reusable inputs to future runs:

```text
packages/core_hang/
  workflow.yaml
  input.template.json
  skills/
  validators/
  README.md
  package_manifest.json
```

The case directory contains real per-run inputs:

```text
cases/
  bug_001.json
  bug_002.json
  bug_003.json
```

The run directory contains execution outputs:

```text
runs/bug_001/
  workflow.yaml
  input.json
  trace.jsonl
  nodes/
  evidence.json
  report.md
```

These should not be mixed.

---

### 7. Minimal doctrine

Keep the interface small:

```text
camflow plan "<intent>" --out DIR
camflow pack SOURCE_DIR --out PACKAGE_DIR
camflow run PACKAGE_DIR/workflow.yaml --input INPUT.json --out RUN_DIR
camflow batch PACKAGE_DIR/workflow.yaml --inputs "cases/*.json" --out BATCH_DIR
```

No install.

No registry.

No package lock.

No package replay.

No package-specific runtime.

No dynamic workflow mutation.

No hidden fallback.

The core rule:

```text
plan creates editable files.
pack cleans them into a reusable directory.
run executes workflow.yaml with real input.json.
```

这个版本把 package 降到最低复杂度：**`pack` 只是 clean copy，不是 package manager**。我建议直接把旧 `.camflowpkg/install/lock/replay` 那套先 archive 掉，v1.2 只保留这个 simple bundle model。

