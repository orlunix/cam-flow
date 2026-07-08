# CamFlow Release Flow

CamFlow releases are one readable executable: `dist/camflow`.

## Build gate

```bash
python3 -m unittest tests.test_camflow_build -v
scripts/build.sh
python3 -m py_compile dist/camflow
./dist/camflow version
```

The generated artifact is a shell/Python polyglot. It invokes
`${CAMFLOW_PYTHON:-python3}` and has a runtime guard for Python 3.6 or newer.
It has no vendored dependencies, extraction cache, release tree, or tarball.

## Deploy

Copy the one artifact to the stable destination and make it executable:

```bash
scp dist/camflow <host>:~/.cam/camflow
ssh <host> 'chmod +x ~/.cam/camflow && ~/.cam/camflow version'
```

Use a canary host first, then deploy the identical `dist/camflow` file to the
remaining targets. The artifact's `version` output includes the commit and
build timestamp for audit.

## Rollback

Keep prior `dist/camflow` artifacts in the normal release archive. Rollback is
a single-file replacement followed by `~/.cam/camflow version` verification.
