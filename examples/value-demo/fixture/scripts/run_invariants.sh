#!/usr/bin/env python3
"""run_invariants — emit a v1.1 envelope JSON to stdout after
running `pytest tests/invariants/` at the project root.

Used by the `invariant_checker` command_runner node in workflow-reference.yaml.
"""
import json
import re
import subprocess
import sys
from pathlib import Path


def find_root(start: Path) -> Path | None:
    p = start.resolve()
    while True:
        if (p / "SPEC.md").exists():
            return p
        if p == p.parent:
            return None
        p = p.parent


def main() -> int:
    root = find_root(Path.cwd())
    if root is None:
        print(json.dumps({
            "status": "fail", "data": {},
            "error": {"code": "NO_PROJECT_ROOT",
                      "message": "SPEC.md not found walking up"},
            "feedback": None, "request_human": False,
        }))
        return 0

    proc = subprocess.run(
        ["pytest", "tests/invariants/", "-q", "--tb=short"],
        cwd=root, capture_output=True, text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode == 0
    m = re.search(r"(\d+) passed", output)
    count = int(m.group(1)) if m else 0

    # Best-effort: extract failed test names from pytest output.
    failed = re.findall(
        r"FAILED (tests/invariants/[^\s]+::[^\s]+)", output
    )

    envelope = {
        "status": "success" if passed else "fail",
        "data": {
            "passed": passed,
            "tests_run": count,
            "failed_tests": failed,
            "output": output[-1500:],
        },
        "error": (None if passed else {
            "code": "INVARIANTS_FAILED",
            "message": f"pytest exit {proc.returncode}; "
                       f"{len(failed)} test(s) failed",
        }),
        "feedback": None,
        "request_human": False,
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
