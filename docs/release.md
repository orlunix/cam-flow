# CamFlow Release Flow

This is the release path for the first remote CamFlow version. It mirrors the
camc / TeaSpirit model: build a relocatable artifact, deploy it to each
registered remote machine, verify the installed binary, and leave an auditable
tag plus archived tarball.

## 1. Release Gate

Run these before any remote deploy:

```bash
PYTHONPATH=src python3 -m pytest tests/ -q
```

For goal-driven and replan changes, also run the oracle-maze live gate against a
fresh oracle session. Pass criteria:

- workflow starts at `dag_revision=1`
- the oracle controlled halt happens at revision 1
- `on_halt: replan` auto-enters Planner without a manual `camflow replan`
- `dag_revisions/0002/manifest.json` records `reason=auto_replan_after_halt`
- revision 2 executes with `CAMFLOW_DAG_REVISION=2`
- final `camflow status` is `state: success`, `dag rev: 2`
- oracle `maze_status.data.solved == true`

The May 5, 2026 release-gate run used:

```text
run dir: /tmp/oracle-maze-releasegate-20260505-061510/maze/.camflow/run
result:  success, dag rev 2, auto-replan used 1/1
tests:   236 passed in 7.80s
```

## 2. Build

```bash
scripts/build.sh
```

Outputs:

- `dist/camflow`
- `dist/camflow-release/`
- `dist/camflow-release.tar.gz`
- `dist/camflow.py`

The wrapper is relocatable and runs `python3 -m runner.runtime` with the release
tree plus vendored runtime dependencies on `PYTHONPATH`. It resolves symlinks
before locating `camflow-release/` and chooses `python3.12`, `python3.11`,
`python3.10`, then `python3`, requiring Python 3.10 or newer. Verify locally:

```bash
dist/camflow version
dist/camflow --help
dist/camflow.py version
dist/camflow.py --help
```

`dist/camflow.py` is the camc-style zero-install single-file launcher. It uses
only the stdlib at startup, re-execs into Python 3.10+ if the default
interpreter is older, extracts an embedded immutable release tree into
`~/.cache/camflow/single-file/<payload-hash>/`, then runs the same runtime with
vendored dependencies. Set `CAMFLOW_SINGLE_FILE_CACHE` to override the cache
location.

## 3. Dry Run

```bash
scripts/release.sh --skip-build --dry-run
```

The release script reads `~/.cam/machines.json`, lists SSH targets, and prints
the exact `ssh` / `scp` / verification actions without touching remotes.

For a single canary:

```bash
scripts/release.sh --skip-build --dry-run --only <machine-name>
```

## 4. Deploy

Use a canary first:

```bash
scripts/release.sh --skip-build --only <machine-name>
```

Then deploy to all registered SSH machines:

```bash
scripts/release.sh --skip-build
```

Remote layout:

```text
~/.cam/camflow
~/.cam/camflow.py
~/.cam/camflow-release/
~/.cam/camflow-release.tar.gz
```

If `/home/prgn_share/bin` is writable, the script also installs:

```text
/home/prgn_share/bin/camflow -> /home/prgn_share/tools/camflow/current/camflow
/home/prgn_share/bin/camflow.py
/home/prgn_share/tools/camflow/current/camflow-release/
```

If `/home/prgn_share/tools/camflow/releases` is writable, it archives the
tarball there.

## 5. Verification

The deploy script verifies every target with:

```bash
~/.cam/camflow version
```

The version string must match the local artifact exactly. A successful non-dry
run creates an annotated git tag:

```text
deploy-YYYYMMDDHHMMSS
```

Inspect it with:

```bash
git show deploy-YYYYMMDDHHMMSS
```

## 6. Rollback

Rollback is artifact based. Copy an older tarball from the shared release
archive or a known good local `dist/camflow-release.tar.gz`, then extract it on
the affected machine:

```bash
scp camflow-release.tar.gz <host>:~/.cam/
ssh <host> 'cd ~/.cam && tar -xzf camflow-release.tar.gz && ~/.cam/camflow version'
```

The wrapper path stays stable, so agents using `~/.cam/camflow` pick up the
rolled-back release without changing their command path.
