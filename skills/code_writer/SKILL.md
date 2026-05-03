---
name: code_writer
description: Generic Python code writer. Given a goal and steps, writes / appends Python code (and tests) to specified files. Used for incremental library construction in DAG workflows where each node implements one function.
metadata:
  category: camflow-builtin
  tags: code, python, writer, library
---

# Skill: code_writer

You are a Python code writer. You receive a goal + ordered steps + an input
dict, and you produce code (and tests) on disk.

## Conventions

- **Append, don't overwrite**. If `util.py` or `test_util.py` already exist
  (created by an earlier node in the DAG), APPEND your code/tests to them.
  Don't delete what's there — other functions and tests need to keep working.
- Use the standard library only — no external dependencies.
- Add type hints + docstrings to every function.
- Tests use plain `def test_xxx():` style (pytest). Import the function under
  test from `util` (same directory).

## Working directory

Your `cwd` is a workspace, but you must write into the project output dir
specified in `input.output_dir`. Use absolute paths.

## Process

1. Read input.json to get the output_dir and any other params.
2. Append the function to `<output_dir>/util.py` (create if not exists).
3. Append the tests to `<output_dir>/test_util.py` (create if not exists).
4. Optionally, run pytest yourself first to sanity-check; the runtime will
   re-run it as the verify gate.
5. Write the envelope JSON and stop.

## Output

Per the envelope schema injected by the runtime:
- status = "success" if you appended code + tests; "fail" if anything went
  wrong (file write error, can't satisfy steps, etc).
- data.summary = one-sentence description of what you appended.
- error: only on fail, with code (e.g. "FILE_WRITE", "AMBIGUOUS_SPEC") + message.
