# A/B Result — value-demo Canonical Attempt 001 — 2026-05-04

> ⚠ **HALTED on a new blocker.** Canonical run did not complete.
> Per `codex-next-after-camc-fix` directive ("if canonical run
> exposes a new blocker, stop and report root cause instead of
> papering it over"), the run was terminated for inspection.

## Run Metadata

- camflow repo: `/home/hren/.openclaw/workspace/camflow` @ commit
  `7ecea42` (current HEAD; camflow-side resilience reframe is staged
  but not yet committed in this round).
- cam repo: `/home/hren/.openclaw/workspace/cam` @ commit `31d8278`
  (includes my `2caca9c` trust-dialog/delayed-send fix + cam-dev's
  `6ebba2c` TOML array parser fix + `31d8278` bracketed-paste).
- Deployed `camc`: `/data/venv/bin/camc` (PATH-resolved; camflow
  invokes this), v1.2.0 (dev). Bundle at `~/.cam/camc` also in sync.
- Claude Code: v2.1.126.
- tmux: 3.4.
- Fresh fixture: `/tmp/camflow-canonical-20260504-081547/camflow`.
- Run dir: `/tmp/camflow-canonical-20260504-081547/camflow/.camflow/run/`.
- Command (run from inside fixture):
  ```
  camflow run "$(cat /home/hren/.openclaw/workspace/camflow/examples/value-demo/PROMPT.txt)"
  ```
  Background-launched; runner pid `826145`.

## Outcome

- **Planner: partial.** `understand` completed first try (success).
  `design_dag` started but the camc-spawned Claude agent
  (`4e9a84d4`) never received its prompt. Sat at the empty `❯ Try
  "fix typecheck errors"` ready screen indefinitely.
- **User workflow: did not start.** The Planner DAG never advanced
  past `design_dag`, so no user-side `workflow.yaml` was rendered
  and no user-side nodes ran.
- **Tests: not applicable.** Code unchanged; `lib/csvparser.py`
  still raises `NotImplementedError`. Visible test fails; invariants
  fail.
- **Runner state at termination: alive but stuck** (the camc
  `wait_for_file` polled indefinitely; would have eventually
  triggered the user-imposed kill).

## Root cause: tmux `send-keys -l --` argument-size limit

The camc `tmux_send_input` (`cam/src/cam/adapters/configs/...` →
`cam/dist/camc:998-1028` and `cam/src/camc_pkg/cli.py`) sends the
entire prompt as a single `tmux send-keys -t <session> -l -- <text>`
call. tmux 3.4 has an argument-size limit somewhere between 16 and
20 KB (verified locally: 16 KB succeeds, 20 KB fails with
`command too long`).

The Planner's `design_dag` node prompt is **20 692 bytes** (460
lines — workflow_designer SKILL.md is large, especially after the
P0/P1 enrichments). That single send-keys call returns non-zero
exit; the Python wrapper logs `tmux_send_input failed: ... returned
non-zero exit status 1` and silently moves on. Claude is left at
its empty input screen; nothing was typed. The runtime then waits
forever on `agent_output.json`.

**Reproducible direct evidence** — manually re-sending the same
prompt to the live agent via `camc send <id> --text "$(cat
prompt.txt)"`:

```
WARNING tmux_send_input failed: Command '['/usr/bin/tmux', '-u',
  '-S', '/tmp/cam-sockets/cam-4e9a84d4.sock', 'send-keys', '-t',
  'cam-4e9a84d4:0.0', '-l', '--', '\x1b[200~# Skill: workflow_designer
  ... <20KB body> ... \x1b[201~']' returned non-zero exit status 1.
```

And a minimal repro against the same socket:

```
$ DUMMY=$(printf 'x%.0s' {1..20000})
$ tmux ... send-keys -t cam-... -l -- "$DUMMY"
command too long
```

vs. 16 KB which succeeds.

This is the third class of camc bug we've found in the same launch
path:

1. v2.1+ trust dialog not auto-confirmed (fixed in `2caca9c`).
2. TOML array parser shattering comma-strings into separate args
   (fixed in `6ebba2c`).
3. Large bracketed-paste payloads exceed tmux argument-size limit
   (this finding — not yet fixed).

## Why the smoke test missed it

My earlier smoke test (`Print exactly READY-LIVE`) and the
reviewer's smoke (`READY-CAMC-SMOKE-CODEX`) used tiny prompts —
~25 chars each — well below the limit. The first time camc was
asked to send a real Planner prompt, it hit the limit. The
camflow `understand` node prompt (which DID succeed in this run)
is smaller than the limit; `design_dag`'s prompt is larger because
it includes the full `workflow_designer` SKILL.md plus Workflow
Context plus upstream `understand` envelope.

## What's required to clear this

**In cam (cross-repo):** chunk large payloads in `tmux_send_input`.
The bracketed-paste markers wrap the *whole* payload; the body
between them can be split across multiple `send-keys -l --` calls
without affecting receiver semantics. Suggested chunk size: 8 KB
(safely under the limit). Pseudocode:

```python
def tmux_send_input(session_id, text, send_enter=True):
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    CHUNK = 8192
    try:
        if text:
            if "\n" in text:
                _run(base + ["send-keys", "-t", target, "-l", "--", "\x1b[200~"], check=True)
                for i in range(0, len(text), CHUNK):
                    _run(base + ["send-keys", "-t", target, "-l", "--", text[i:i+CHUNK]], check=True)
                _run(base + ["send-keys", "-t", target, "-l", "--", "\x1b[201~"], check=True)
            else:
                for i in range(0, len(text), CHUNK):
                    _run(base + ["send-keys", "-t", target, "-l", "--", text[i:i+CHUNK]], check=True)
        if send_enter:
            ...
```

(Files: `cam/src/camc_pkg/cli.py`'s `tmux_send_input` plus the
mirror in the bundled `dist/camc`. Then `python build_camc.py` and
`cp dist/camc ~/.cam/camc`.)

This is small, low-risk, and self-validating: rerun the canonical
camflow → `design_dag` actually receives its prompt → Planner
produces a workflow.yaml → user-workflow runs.

## Score

Not scored. The run did not complete. Scoring at this point would
mis-attribute a camc plumbing failure to camflow-level structural
quality.

## Camflow-side changes staged in this round (not committed yet)

The reviewer asked for resilience-semantics rework regardless of
the live-run outcome. These are ready to commit but I'm holding
until the cam fix lands so the next canonical rerun can validate
both:

- `examples/value-demo/scripts/score.py` — rubric key
  `recovery` → `resilience`; new auto-scoring rule (5/5 first-pass
  done + bounded retry configured; 4/5 clean halt with feedback;
  3/5 done without bounded retry; 1/5 halt without feedback;
  churn penalty); new `nodes_with_bounded_retry` /
  `halt_has_actionable_feedback` artifacts in the camflow detection
  block.
- `examples/value-demo/AB-PROTOCOL.md` — score-table row renamed
  to `resilience`; explanatory note clarifying retry-as-safety-net
  semantics per the user correction.
- `builtin/planner/skills/workflow_designer/SKILL.md` — softened
  "retry-with-feedback fires on missed requirements" to
  "supports retry-with-feedback if verification fails"; explicit
  "retry is a safety net, not a feature" framing in the recipe and
  the retry-rule.
- `tests/test_assets.py` — sentinels still pass after softening.
- `tests/test_e2e_value.py` — three new resilience tests
  (first-pass done with bounded retry → 5/5; done without bounded
  retry → 3/5; clean halt with feedback → 4/5).

Suite at 170/170 green locally. Will commit these after the cam
fix lands and the next canonical run validates the end-to-end
behavior.

## Next steps

1. **cam-dev:** chunk `tmux_send_input` payloads above ~8KB. See
   pseudocode above. Smoke test with a >20KB prompt before signing
   off — the existing smoke prompts are too small to exercise this
   path.
2. **camflow re-run** (you, after fix lands): same canonical
   command from a fresh fixture.
3. **camflow commit** (me, after canonical rerun produces a real
   score): land the staged resilience-semantics changes.

## Termination details

- Sent `kill -TERM 826145` at ~`08:23:30 PDT 2026-05-04`. Runner
  exited cleanly within 3s; the runtime's `kill_by_tag` swept the
  child design_dag agent (`4e9a84d4`) automatically. No leftover
  agents. Run dir preserved at
  `/tmp/camflow-canonical-20260504-081547/camflow/.camflow/run/` for
  post-mortem inspection.
