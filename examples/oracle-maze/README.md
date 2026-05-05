# oracle-maze — Phase A halt + replan benchmark

Exercises CamFlow's halt-time replan path with a real Planner and
real Runtime against a black-box HTTP oracle. The oracle deliberately
halts the first submit on `dag_revision=1` even when the path is
correct; a successful solve must come from `dag_revision >= 2`,
which CamFlow only reaches via `camflow replan` recording a new DAG
revision after the halt.

This benchmark is **separate** from `examples/value-demo/` — that one
stays as the canonical scoring benchmark. This one specifically tests
the replan/revision-bumping plumbing.

## Layout

```
examples/oracle-maze/
├── README.md                # this file
├── PROMPT.txt               # the task seen by Planner
├── workflow-reference.yaml  # plausible shape (informational)
└── scripts/
    ├── maze_scan.sh         # tool wrapper → envelope JSON
    ├── maze_probe.sh        # tool wrapper → envelope JSON
    ├── maze_submit.sh       # tool wrapper → envelope JSON
    └── maze_status.sh       # tool wrapper → envelope JSON
```

## Running it (manual / costed)

The oracle runs locally on `http://127.0.0.1:8765`. Required env:

```bash
export CAMFLOW_ORACLE_BASE_URL=http://127.0.0.1:8765
export CAMFLOW_ORACLE_SESSION_ID=<your-session>
```

(The wrappers refuse to run if either is unset.)

Quick wrapper-level smoke (no LLM cost):
```bash
printf '%s' '{}' | bash scripts/maze_status.sh | jq .
```

Full live run via real Planner + Runtime:
```bash
PFX=/tmp/oracle-maze-$(date +%Y%m%d-%H%M%S)
mkdir -p "$PFX"
cp -r examples/oracle-maze "$PFX/maze"
( cd "$PFX/maze" && \
    camflow run "$(cat PROMPT.txt)" )
# Expected: halts at solver. halt.json mentions ORACLE_HALT.
camflow replan "$PFX/maze/.camflow/run"
# Expected: dag_revisions/0002/ created; solver re-runs at rev 2;
# oracle accepts; workflow ends done.
```

Verify:
```bash
camflow status --run-dir "$PFX/maze/.camflow/run"
ls "$PFX/maze/.camflow/run/dag_revisions/"
# 0001/ and 0002/ both present
```

## What this benchmark proves

- `dag_revision` is plumbed into tool subprocesses as
  `CAMFLOW_DAG_REVISION` (env) and `dag_revision` (input.json).
- A halted run can be re-planned via `camflow replan`, which:
  - re-invokes the existing builtin Planner workflow with halt
    context appended to the original prompt;
  - records the new compiled workflow.yaml under
    `dag_revisions/<N>/` with `parent_revision = <N-1>` and
    `reason = manual_replan_after_halt`;
  - archives the prior `nodes/` + `halt.json` into the prior
    revision's slot for replay;
  - re-executes the new DAG, which can now succeed because the
    submit happens at `dag_revision >= 2`.
- `camflow status` correctly reports the active revision after replan.

## What this benchmark does NOT prove (Phase B)

- **Auto-replan** — Phase A is manual only. The runtime does NOT
  re-invoke Planner on its own when a workflow halts.
- **Invalidation rules** — Phase A re-runs all nodes in the new
  revision (conservative). A real invalidation analysis (which
  upstream outputs survive a replan, which downstream nodes must
  re-run) is Phase B work.
- **Long-lived Planner residency** — Planner is re-invoked
  (logically resident, in the supplement's words), not kept as a
  resident process.

These remain explicit gaps; do not claim full Planner residency or
auto-replan are complete.
