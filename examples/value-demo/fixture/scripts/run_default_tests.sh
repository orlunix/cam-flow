#!/usr/bin/env python3
"""run_default_tests — emit a v1.1 envelope JSON to stdout after
running `pytest tests/` (excluding tests/invariants/) at the
project root.

Used by the `test_runner` command_runner node in workflow-reference.yaml.
Walks up from cwd to find SPEC.md (the project marker) so the script
works whether invoked directly or by the runtime from inside an
attempt-N/ subdir.
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
        return 0  # tool emits envelope; non-zero exit is for catastrophic.

    proc = subprocess.run(
        ["pytest", "tests/", "-q", "--tb=short",
         "--ignore=tests/invariants"],
        cwd=root, capture_output=True, text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode == 0
    m = re.search(r"(\d+) passed", output)
    count = int(m.group(1)) if m else 0

    envelope = {
        "status": "success" if passed else "fail",
        "data": {
            "passed": passed,
            "tests_run": count,
            "output": output[-1500:],
        },
        "error": (None if passed else {
            "code": "TESTS_FAILED",
            "message": f"pytest exit {proc.returncode}",
        }),
        "feedback": None,
        "request_human": False,
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
