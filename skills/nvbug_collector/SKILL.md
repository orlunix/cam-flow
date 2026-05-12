---
name: nvbug_collector
description: Collect and normalize NVIDIA NVBugs for RISC-V / rs_riscv_top debug triage. Use as the first CamFlow node when a workflow starts from NVBug keywords, bug IDs, RN102/RISC-V/XRISCV keywords, rs_riscv_top, bug-list collection, or bug triage.
metadata:
  category: camflow-debug
  tags: nvbugs, riscv, triage, collector
disable-model-invocation: false
---

# Skill: nvbug_collector

Collect the bug list before any debug, trace, waveform, or root-cause
analysis. This skill is the canonical first node for NVBugs/RISC-V debug
workflows.

## Procedure

1. Extract the collection target from the node goal, steps, and workflow
   context: explicit bug IDs, bracketed synopsis fragments, module names
   such as `rs_riscv_top`, product tokens such as `rn102n`, `rn102g`,
   `rn102s`, and formal tokens such as `FV`, `FN100`, `XRISCV`.
2. Check `command -v nvbugs-cli`. If missing, write a fail envelope with
   `error.code = "NVBugsCliMissing"` and explain the missing command.
3. Use `nvbugs-cli search bugs --criteria ... --json` as the primary
   collection path. Do not rely on `search by-synopsis` for bracketed
   strings; it has returned false negatives for known RISC-V bugs.
4. Prefer synopsis/module/status criteria first, then fetch details or
   comments for enrichment. Do not make `Keywords=...` the primary
   collection axis; it has produced NVBugs advanced-search backend errors
   for `Remap:IP:MMPLEX:riscv`.
5. Deduplicate by bug id. Keep enough metadata for downstream triage:
   bug id, synopsis, action/status, severity, days/open age when present,
   keywords, engineer, ARB/blocker owner, source query, and a coarse bucket
   such as `Simulation` or `FV`.
6. Write `agent_output.json` with the fields required by the node's
   `output_schema`.

## Query patterns

Use wildcard `--criteria` patterns that match how NVBugs stores the
synopsis text.

- Exact bracket fragment:
  `--criteria 'Synopsis=*[rn102n][rn102n_mse][rs_riscv_top][*'`
- Split RN102/RISC-V module search:
  `--criteria 'Synopsis=*rn102*' --criteria 'Synopsis=*rs_riscv_top*'`
- Narrow RN102N MSE cluster:
  `--criteria 'Synopsis=*rn102n_mse*' --criteria 'Synopsis=*rs_riscv_top*'`
- Broad simulation collection:
  `--criteria 'Synopsis=*rs_riscv_top*' --status 'HW - Open - To fix'`
- FV/XRISCV collection:
  `--criteria 'Synopsis=*FV*' --criteria 'Synopsis=*FN100*' --criteria 'Synopsis=*XRISCV*' --status 'HW - Open - To fix'`

If an exact-looking query returns zero rows, run a broader smoke query
before concluding there are no bugs.

## Output Contract

Match the workflow node's schema exactly. Recommended fields are:

- `bug_ids` (array) — unique bug IDs as strings.
- `bugs` (array) — normalized bug records.
- `count` (integer) — number of unique bugs.
- `queries` (array) — commands or criteria groups that produced results.
- `collection_complete` (boolean) — true only if collection finished
  without a tool/backend failure.
- `notes` (array) — caveats such as keyword-query fallback or missing
  enrichment fields.

When no bugs match but the CLI worked, return success with empty arrays,
`count: 0`, `collection_complete: true`, and a note naming the exact
criteria used.

## On retry

Read `previous.feedback`. If downstream verification says the list is too
narrow, broaden by synopsis/module tokens first. If it says duplicates or
wrong scope leaked in, deduplicate by bug id and add the rejected criteria
to `notes` so downstream nodes can see why the collection changed.
