"""Git-based checkpoints — local mode.

After a successful agent node, auto-commit the project directory so
every fix becomes a recoverable atomic step. Best-effort: never blocks
the workflow if git isn't available or the commit fails.

Roadmap: §6.1 Checkpoint System — Git-based checkpoints (local mode).
"""

import subprocess


def _run(args, cwd, timeout=10):
    """Run a git command silently; return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return -1, "", str(exc)


def checkpoint_after_success(project_dir, node_id, iteration, summary):
    """Auto-commit the project directory after a successful agent node.

    Steps:
      1. `git init` (no-op if already a repo).
      2. `git add -A` to stage everything.
      3. `git commit -m "camflow: <node_id> iter <N> — <summary>"
         --allow-empty` so checkpoint always lands, even if nothing
         was touched (important for restore markers).

    Never raises. Never blocks the caller. If git is missing or the
    commit fails, this function returns silently and the workflow
    proceeds as if checkpointing were disabled.

    Args:
        project_dir: directory to commit
        node_id: the workflow node that just succeeded
        iteration: monotonic engine step counter
        summary: one-line description from node_result

    Returns:
        True if the commit ran without error, False otherwise.
        (Callers may ignore; this is for tests and logging.)
    """
    try:
        _run(["git", "init"], cwd=project_dir, timeout=5)
        _run(["git", "add", "-A"], cwd=project_dir, timeout=10)
        msg = f"camflow: {node_id} iter {iteration} — {summary or '(no summary)'}"
        rc, _out, _err = _run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=project_dir, timeout=10,
        )
        return rc == 0
    except Exception:
        return False
