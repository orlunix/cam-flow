# CamFlow v1.2 RFC: Packaged Workflows

Status: draft RFC for the next CamFlow version after v1.1

Owner: CamFlow

## 1. Summary

CamFlow v1.1 compiles a natural-language task through the builtin
Planner every time a fresh run starts. That is the right default for
new, ambiguous work, but it is not the right default after a workflow
has been tuned, tested, and proven valuable.

CamFlow v1.2 should add **packaged workflows**: an installable,
content-addressed bundle that freezes a proven worker flow so the
runtime can execute it directly without invoking Planner first.

The package should include:

- compiled `workflow.yaml`
- all required `skills/<name>/SKILL.md`
- optional relative tools/scripts used by `run.tool`
- package manifest and lockfile
- optional node prompt source snapshots for audit/reproducibility
- optional verifier prompt source snapshots where applicable
- parameter schema, environment/tool requirements, and preflight checks
- benchmark/evidence metadata from the run that produced the package

The workflow inside the package can still target `version: "1.1"`.
The new version boundary is the **package/install/runtime interface**,
not necessarily the workflow YAML grammar.

P0 CLI principle: keep the existing `camflow run` contract stable. The
only new run-time selector should be `camflow run --package
<name>@<version>` (or an optional short `-p` alias after conflict
check). All package creation, validation, inspection, and installation
belongs under `camflow package ...`.

## 2. Current Version Position

Current CamFlow repo state:

- Python package version: `1.1.0` (`pyproject.toml`)
- Workflow spec: `docs/spec.md` v1.1
- README status: "Version 1.1"
- v1.1 execution model: source-tree runtime, builtin Planner first for
  fresh natural-language runs

Recent work already moved beyond the original v1.1 baseline in runtime
capabilities, especially goal-driven retry, replan, auto-replan, remote
release, and managed child-agent names. Packaged workflows are a clean
next version topic. Recommend:

- CamFlow package/release version: `1.2.0`
- Workflow YAML spec inside packages: still `version: "1.1"` initially
- New package manifest version: `package_schema: "1"`

This avoids destabilizing v1.1 workflow semantics while still giving the
product a real v1.2 feature.

## 3. Goals

1. **Skip Planner for proven workflows.** An installed package runs from
   node 1 directly.
2. **Make tuned flows reusable.** A workflow tuned on one machine can be
   installed and run on another compatible machine.
3. **Make runs reproducible.** Every run records the package content
   digest, workflow digest, skill digests, prompt source digests when
   present, runtime version, and environment preflight result.
4. **Keep replan.** A packaged workflow may still halt and replan. The
   replan should start from the frozen package baseline and record a new
   DAG revision.
5. **Avoid hidden mutable dependencies.** Package install must not rely
   on whatever happens to be in the current source tree's `skills/` or
   `builtin/`.
6. **Preserve v1.1 strictness.** Missing skills/tools fail at load time.
   Unknown package fields are explicit validation errors until allowed.

## 4. Non-Goals

- Do not replace Planner for ad hoc fresh prompts.
- Do not introduce a resident orchestrator.
- Do not add skill multi-versioning inside the source tree.
- Do not solve cross-company credential distribution.
- Do not make packages self-modifying.
- Do not carry over attempt outputs as valid future inputs unless a
  package explicitly declares fixtures/examples.

## 5. Package Artifact

Use one deterministic archive file. Default extension:

```text
<name>-<version>.camflowpkg
```

Internally it is a gzipped tar for v1.2 because Python stdlib can read
and write it everywhere. The package is a **distribution artifact**,
not just a copied run directory:

```text
camflowpkg/
  manifest.yaml
  lock.json
  workflow.yaml
  skills/
    analyzer/SKILL.md
    code_writer/SKILL.md
    reviewer/SKILL.md
  tools/
    ...
  prompts/
    nodes/<node_id>/run.md
    nodes/<node_id>/verify.md
  preflight/
    checks.yaml
  examples/
    input.example.yaml
  evidence/
    source-run.json
    benchmark.md
```

The top-level archive root must be exactly `camflowpkg/`.

### 5.1 Normative Package Format v1

For v1.2, a CamFlow package is exactly:

```text
file extension:     .camflowpkg
media type:         application/vnd.camflow.package+tar
archive format:     gzip-compressed POSIX tar
archive root:       camflowpkg/
text encoding:      UTF-8 for YAML, JSON, Markdown, and text prompts
package schema:     package_schema: "1"
workflow spec:      workflow.yaml version: "1.1" for P0
```

The archive MUST be deterministic:

- paths sorted lexicographically
- file mode normalized to `0644`, directories to `0755`
- uid/gid normalized to `0`
- owner/group normalized to empty or `root`
- mtimes normalized to `0` or the manifest `created_at` timestamp
- no symlinks, hardlinks, device files, or absolute paths
- no path component may be `..`
- no duplicate paths after normalization
- no files outside the `camflowpkg/` root

Required files:

```text
camflowpkg/manifest.yaml
camflowpkg/lock.json
camflowpkg/workflow.yaml
```

Required directories:

```text
camflowpkg/skills/
```

P0 package creation MUST fail if any required path is missing. Empty
required directories are not useful: `skills/` must contain every local
skill referenced by the workflow.

Optional directories:

```text
camflowpkg/prompts/
camflowpkg/tools/
camflowpkg/preflight/
camflowpkg/examples/
camflowpkg/evidence/
camflowpkg/docs/
```

`manifest.yaml` is the authored contract. `lock.json` is generated by
`camflow package create` and is the integrity contract. `workflow.yaml`
is the frozen DAG entry point.

### 5.2 File Layout v1

Canonical layout:

```text
camflowpkg/
  manifest.yaml
  lock.json
  workflow.yaml
  skills/
    <skill_name>/SKILL.md
  tools/
    <relative_tool_path>
  prompts/
    nodes/
      <node_id>/
        run.md
        verify.md
  preflight/
    checks.yaml
  examples/
    input.example.yaml
  evidence/
    source-run.json
    benchmark.md
  docs/
    README.md
```

Rules:

- `skills/<skill_name>/SKILL.md` is required for every `run.skill`
  referenced by `workflow.yaml`, unless the manifest explicitly declares
  an external skill dependency.
- `tools/` paths may only satisfy `run.tool` references declared in
  `workflow.yaml`; they must remain relative to package root and must be
  declared in `manifest.yaml`.
- `prompts/` is optional audit data. Runtime may ignore it during normal
  execution.
- `prompts/nodes/<node_id>/run.md` SHOULD exist for every node when
  `prompt_snapshots.enabled: true`.
- `prompts/nodes/<node_id>/verify.md` SHOULD exist for every node with
  agent verification when `prompt_snapshots.enabled: true`.
- `evidence/` is descriptive only; runtime must not treat it as node
  state.
- `examples/` may contain sample parameter files, but not secrets.
- `docs/` is descriptive only. Runtime must not load executable behavior
  from `docs/`.

### 5.3 Path Grammar

Package paths are normalized POSIX relative paths below `camflowpkg/`.
After stripping the root prefix, every path must match one of these
families:

```text
manifest.yaml
lock.json
workflow.yaml
skills/<skill_name>/SKILL.md
tools/<path>
prompts/nodes/<node_id>/run.md
prompts/nodes/<node_id>/verify.md
preflight/checks.yaml
examples/<path>
evidence/<path>
docs/<path>
```

Name grammar:

```text
package name: [a-z][a-z0-9_]{2,63}
skill name:   [a-z][a-z0-9_]{1,63}
node id:      [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}
```

P0 should reject files outside these families. This gives the first
implementation a small, auditable attack surface. New path families can
be added by bumping `package_schema`.

### 5.4 Package Identifier

Package identity is:

```text
<name>@<version>
```

where:

- `name` matches `[a-z][a-z0-9_]{2,63}`
- `version` is SemVer-like: `MAJOR.MINOR.PATCH[-suffix]`

The content identity is:

```text
sha256:<content-digest>
```

Runtime should display both:

```text
package: peregrine_rv30_verification@0.1.0 sha256:...
```

Name/version is for humans. Digest is for reproducibility.

Runtime and install identity should be:

```text
<name>@<version>#sha256:<content-digest>
```

The resolver must never silently replace one digest with another for the
same `<name>@<version>`. Installing a different digest under an existing
name/version should require an explicit replace flag.

### 5.5 Minimal Valid Package

The smallest valid P0 package is:

```text
camflowpkg/
  manifest.yaml
  lock.json
  workflow.yaml
  skills/<skill_name>/SKILL.md
```

It is valid only if:

- `workflow.yaml` references exactly the packaged skill(s)
- if `prompt_snapshots.enabled: true`, every node in `workflow.yaml` has
  a prompt snapshot or an explicit manifest reason for missing one
- `lock.json` includes all files except itself
- `content_digest` recomputes cleanly
- no preflight requirement is unmet at run start

Everything else (`prompts/`, `tools/`, `examples/`, `evidence/`,
`docs/`) is optional and must be ignored by runtime unless explicitly
declared by the manifest.

## 6. Manifest

`manifest.yaml` is the human-readable contract.

Example:

```yaml
package_schema: "1"
name: peregrine_rv30_verification
version: "0.1.0"
description: "Run the tuned Peregrine 5D1 RISC-V RV30 verification flow."
workflow_spec: "1.1"
workflow_entry: workflow.yaml

authors:
  - hren

tags:
  - peregrine
  - rv30
  - verification
  - coremark

runtime:
  min_camflow: "1.2.0"
  child_agent_tool: codex
  planner_required_for_initial_run: false
  replan_supported: true
  package_local_skills: true

parameters:
  workspace_root:
    type: path
    required: true
    description: "Existing or target Peregrine workspace root."
  p4_depot:
    type: string
    default: "//hw/nvip/..."
  build_target:
    type: string
    default: "rn102g_fecs_peregrine5d1"

environment:
  required_env:
    - P4PORT
    - P4USER
  required_commands:
    - p4
    - qsub
    - smake

skills:
  analyzer:
    path: skills/analyzer/SKILL.md
    digest: "sha256:..."
  code_writer:
    path: skills/code_writer/SKILL.md
    digest: "sha256:..."
  reviewer:
    path: skills/reviewer/SKILL.md
    digest: "sha256:..."

tools: []

prompt_snapshots:
  enabled: true
  path: prompts/nodes

provenance:
  source_run_dir: "/path/to/.camflow/run"
  source_camflow_commit: "<git-sha>"
  created_at: "2026-05-07T00:00:00Z"
  created_by: "camflow package create"
```

### 6.1 Manifest Field Rules

Required top-level fields:

```yaml
package_schema: "1"
name: <package_name>
version: <package_version>
workflow_spec: "1.1"
workflow_entry: workflow.yaml
runtime:
  min_camflow: "1.2.0"
skills: {}
provenance: {}
```

Recommended fields:

```yaml
description: string
authors: [string]
tags: [string]
parameters: {}
environment: {}
tools: []
prompt_snapshots: {}
```

Unknown top-level fields should fail validation in v1.2. This is
intentional: packages are a distribution contract, and silent unknowns
make them hard to reproduce.

Field constraints:

- `package_schema` must be the string `"1"`.
- `workflow_spec` must be the string `"1.1"` for P0.
- `workflow_entry` must be exactly `workflow.yaml` for P0.
- `runtime.min_camflow` must be a concrete version, not a range.
- `runtime.planner_required_for_initial_run` must be `false` for a
  package intended for direct execution.
- `runtime.package_local_skills` defaults to `true`; P0 should reject
  `false`.
- every `skills.<name>.path` must be `skills/<name>/SKILL.md`.
- every declared tool path must live under `tools/`.
- environment declarations may name required variables, but must not
  contain variable values.
- if `prompt_snapshots.enabled` is `false`, `prompt_snapshots.reason`
  must be present.

Parameter field grammar for P0:

```yaml
parameters:
  <name>:
    type: string | integer | number | boolean | path
    required: true | false
    default: <scalar>        # optional
    description: string     # optional
```

P0 should support parameter declaration and validation, but not require
all packages to use parameters. To keep `camflow run` stable, P0 should
not add ad hoc `--param` flags. A later version can add one explicit
input file, for example `camflow run --package foo@1.0.0 --input
params.yaml`, after the package format proves useful.

## 7. Lockfile

`lock.json` is machine-readable and content-addressed. It includes
SHA256 digests for every package file except `lock.json` itself and a
stable package content digest.

Example:

```json
{
  "package_schema": "1",
  "name": "peregrine_rv30_verification",
  "version": "0.1.0",
  "content_digest": "sha256:...",
  "digest_algorithm": "camflow-tree-sha256-v1",
  "files": {
    "manifest.yaml": "sha256:...",
    "workflow.yaml": "sha256:...",
    "skills/code_writer/SKILL.md": "sha256:...",
    "prompts/nodes/sync_and_build_rn_fecs_simv/run.md": "sha256:..."
  },
  "created": {
    "camflow_version": "1.2.0",
    "camflow_commit": "...",
    "python": "3.12.x"
  }
}
```

The runtime records the package content digest in:

- `.camflow/run/package.json`
- `trace.jsonl` `workflow_started`
- every `dag_revisions/<N>/manifest.json`

### 7.1 Lockfile Field Rules

Required fields:

```json
{
  "package_schema": "1",
  "name": "...",
  "version": "...",
  "content_digest": "sha256:...",
  "digest_algorithm": "camflow-tree-sha256-v1",
  "files": {},
  "created": {}
}
```

`files` maps package-relative paths to SHA256 digests. It must include
every regular file except `lock.json` itself.

P0 uses this canonical tree digest:

```text
content_digest = sha256(
  for each path in sorted(files.keys()):
    utf8(path) + "\0" + utf8(files[path]) + "\n"
)
```

This avoids self-referential archive digest problems and is stable
across tar implementations.

`archive_digest` is optional metadata for the exact `.camflowpkg` file
bytes. Runtime reproducibility should use `content_digest`; transport
integrity may additionally use `archive_digest`.

Install validation order:

1. unpack archive metadata without extracting outside a temp directory
2. reject disallowed tar entry types and unsafe paths
3. parse `manifest.yaml`
4. parse `lock.json`
5. verify manifest name/version/schema match lockfile
6. verify every regular file appears in `files`
7. verify every digest
8. recompute `content_digest`
9. validate workflow skill/tool references against manifest and package
10. materialize install directory atomically

## 8. Prompt Snapshots

CamFlow prompts are partially dynamic because upstream node outputs are
injected at runtime. A package therefore should not claim that every
future prompt byte is precomputed.

Prompt snapshots are optional audit data, not an execution dependency
for normal package runs. Before halt/replan, runtime can execute from
`workflow.yaml` plus package-local skills/tools; it only renders the
current node prompt when that node is about to run.

When present, v1.2 freezes **prompt source snapshots**:

- skill text as packaged
- workflow context
- node goal/steps/schema/verify config
- runtime prompt template version
- placeholder markers for dynamic upstream data

Example prompt snapshot:

```text
# Skill: code_writer
<packaged SKILL.md content>

# Workflow Goal
<packaged workflow.goal>

# Workflow Context
<packaged workflow.context>

# Goal
<node.goal>

# Steps
<node.steps>

# Upstream Inputs
{{ runtime-injected upstream outputs }}

# Output
<runtime envelope contract>
```

This gives reproducibility at the right layer: stable prompt recipe plus
runtime trace of actual dynamic data. If snapshots are omitted, the run
is still valid, but auditability is weaker.

## 9. Install Layout

Default user install:

```text
~/.camflow/packages/<name>/<version>/
  camflowpkg/
  installed.json
```

Project-local install:

```text
./.camflow/packages/<name>/<version>/
```

Resolution order:

1. explicit path supplied to CLI
2. project-local installed package
3. user installed package
4. system/shared package directory, if configured

The resolver must not silently pick a different version. If the user
omits a version and more than one version is installed, fail with a
clear list.

`installed.json` is local install metadata, not part of the package
content digest:

```json
{
  "name": "peregrine_rv30_verification",
  "version": "0.1.0",
  "content_digest": "sha256:...",
  "archive_digest": "sha256:...",
  "installed_at": "2026-05-07T00:00:00Z",
  "installed_by_camflow": "1.2.0",
  "source": "/path/to/peregrine_rv30_verification-0.1.0.camflowpkg"
}
```

The install directory should be content-checked on every package run.
If any installed file no longer matches `lock.json`, runtime should fail
before starting node execution.

## 10. CLI Surface

P0 commands:

```bash
camflow package create --from-run .camflow/run --out dist/foo.camflowpkg
camflow package inspect dist/foo.camflowpkg
camflow package validate dist/foo.camflowpkg
camflow package install dist/foo.camflowpkg
camflow package list
camflow package uninstall <name>@<version>
camflow run --package <name>@<version>
```

Allowed `run` surface change:

```bash
camflow run --package <name>@<version>
```

An optional short alias `-p` can be added only after checking there is no
existing conflict. Do not add `--param`, `--trust-tools`, registry
selectors, or other run flags in P0. If package inputs become necessary,
add one future flag such as `--input params.yaml` rather than many small
flags.

The `camflow package` command is new and can have the management
subcommands it needs. The existing `camflow run "<prompt>"` path remains
the Planner-first v1.1 behavior.

## 11. Runtime Semantics

Fresh package run:

1. Resolve installed package.
2. Validate manifest and lockfile.
3. Materialize run dir.
4. Copy `workflow.yaml` into `.camflow/run/workflow.yaml`.
5. Copy package metadata into `.camflow/run/package.json`.
6. Resolve skills/tools from the package first.
7. Run preflight checks.
8. Execute node scheduling from the first normal node.

Planner is not invoked.

Trace must include:

```json
{
  "event": "workflow_started",
  "package": {
    "name": "peregrine_rv30_verification",
    "version": "0.1.0",
    "content_digest": "sha256:..."
  },
  "planner_invoked": false
}
```

## 12. Replan Semantics

Replan still works, but with package-aware context.

If a packaged workflow halts and `on_halt: replan` is enabled, runtime
invokes Planner with:

- original package manifest
- package content digest
- frozen `workflow.yaml`
- halt info
- prior DAG revision manifest
- current node outputs
- package policy: what may and may not change

The new DAG revision is stored exactly as today:

```text
dag_revisions/0002/
  workflow.yaml
  manifest.json
```

`manifest.json` should add:

```json
{
  "reason": "auto_replan_after_halt",
  "parent_package": {
    "name": "...",
    "version": "...",
    "content_digest": "sha256:..."
  }
}
```

The live rev2 workflow is no longer identical to the installed package,
so status should show both:

```text
package: peregrine_rv30_verification@0.1.0 sha256:...
dag rev:  2 (replanned from package)
```

## 13. Skill Resolution

Package skill resolution must be explicit:

1. package-local `skills/<name>/SKILL.md`
2. optional declared external skill dependency by digest
3. fail

Do not fall back to source-tree `skills/` unless the manifest explicitly
declares:

```yaml
skill_resolution:
  allow_host_skills: true
```

Default should be `false` for reproducibility.

## 14. Security And Trust

Installer must reject:

- absolute paths inside archive
- `..` paths
- symlinks in v1.2
- hardlinks, device files, FIFOs, sockets, and duplicate paths
- files outside the allowed path families
- executable tools unless explicitly allowed by manifest
- missing lock entries
- digest mismatch
- unknown manifest fields
- undeclared workflow skill/tool references
- packaged values that look like credentials or tokens

Installer should print a trust summary:

```text
Package: peregrine_rv30_verification@0.1.0
Digest:  sha256:<content-digest>
Skills:  analyzer, code_writer, reviewer
Tools:   none
Requires commands: p4, qsub, smake
Requires env: P4PORT, P4USER
Install? [y/N]
```

No secrets may be packaged. Env var names are allowed; env var values
are not.

## 15. Export From A Proven Run

`camflow package create --from-run` should only succeed if:

- run state is `success`, unless `--allow-halted` is explicit
- `workflow.yaml` validates
- every referenced skill/tool can be copied or resolved
- package has a name and version
- all files fit path containment rules
- no env var values or known token patterns are found in packaged files

The command should copy:

- final live `workflow.yaml`
- packaged skills and tools
- prompt source snapshots for each node, when available
- source run trace summary
- benchmark/evidence file if supplied

It should not copy:

- node `agent_output.json` as future runtime state
- transient `nodes/attempt-*` directories
- local credentials
- full child-agent transcripts by default

## 16. Validation Gate

Minimum deterministic tests:

1. Build package from a tiny successful run.
2. Install package.
3. Run package and assert Planner was not invoked.
4. Assert package-local skill is used even if source-tree skill differs.
5. Assert missing package skill fails load.
6. Assert package content digest appears in status and trace.
7. Assert replan from packaged workflow creates `dag_revisions/0002`.
8. Assert archive path traversal and symlink payloads are rejected.

Live gate:

- package the oracle-maze workflow after a successful tuned run
- install it into a fresh temp project
- run package against a fresh oracle session
- confirm it starts directly at node execution, no initial Planner
- confirm auto-replan still solves the maze

## 17. Implementation Plan

### P0: Local Frozen Package

Scope:

- `camflow package create`
- `camflow package validate`
- `camflow package inspect`
- `camflow package install`
- `camflow package list`
- `camflow package uninstall`
- `camflow run --package <name>@<version>`
- package-local skill resolution
- package content digest in trace/status
- no remote registry
- no signatures
- no ad hoc run parameter flags

This is the smallest useful v1.2.

### P1: Remote/Shared Install

Scope:

- deploy package to `/home/prgn_share/tools/camflow/packages`
- install from local path on PDX
- run package remotely with release artifact
- update `docs/release.md`

### P2: Package-Aware Replan

Scope:

- pass package manifest/lock into Planner replan context
- record parent package in `dag_revisions/<N>/manifest.json`
- status displays "replanned from package"

### P3: Catalog And Signing

Scope:

- package index
- optional signing
- provenance attestation
- package promotion flow

## 18. Open Questions

1. Should package-local tools be executable by default, or require an
   explicit `--trust-tools` install flag?
2. Should package versions be semantic versions only, or allow content
   digests as the primary selector?
3. Should prompt snapshots include complete runtime templates, or only
   hashes of the runtime template version?
4. How much source-run evidence is safe to package by default?
5. When parameters become necessary, should the only new run flag be
   `--input params.yaml`?

## 19. Recommendation

Make packaged workflows the headline CamFlow v1.2 feature.

Do **not** change v1.1 workflow YAML yet. Add a package layer around the
existing workflow contract, make packages immutable and installable, and
run them directly through the existing Runtime. Keep Planner for fresh
prompt compilation and package-aware replan.

This gives CamFlow a clear second mode:

```text
camflow run "<new ambiguous task>"
  -> Planner compiles, then Runtime executes

camflow run --package <proven-flow>@<version>
  -> Runtime executes frozen DAG immediately
```

That separation is the simplest path to reproducibility, reuse, remote
install, and benchmarkable production workflows.
