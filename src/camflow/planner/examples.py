"""Few-shot examples for the planner prompt.

Three canonical workflows spanning the complexity range:

  1. CALC_DEMO  — simple fix→test loop (calculator demo)
  2. BUILD      — medium: build, lint, smoke-test (verify at each step)
  3. INVESTIGATE — complex: multi-step investigation with state handoff

The examples demonstrate the plan-priority conventions we want the
model to follow: every agent node declares methodology / escalation_max
/ allowed_tools / verify, every loop has max_retries, state handoff is
explicit.
"""


CALC_DEMO = """\
# Example 1: fix→test loop (simple)
# Goal: fix bugs in calculator.py until pytest passes

start:
  do: cmd python3 -m pytest test_calculator.py -v
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: done

fix:
  do: agent claude
  methodology: rca
  escalation_max: 3
  allowed_tools: [Read, Edit, Bash]
  max_retries: 3
  verify: test -f calculator.py
  with: |
    Fix one bug in calculator.py based on the pytest output in the
    CONTEXT block. ONE bug per call — keep the fix minimal.
    Report files_touched and a one-line summary.
  next: test

test:
  do: cmd python3 -m pytest test_calculator.py -v
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: done

done:
  do: cmd echo "all tests pass"
"""


BUILD = """\
# Example 2: build + lint + smoke test (medium)
# Goal: compile, lint, and smoke-test a Python package before release

start:
  do: cmd python3 -m build --wheel
  transitions:
    - if: fail
      goto: fix_build
    - if: success
      goto: lint

fix_build:
  do: agent claude
  methodology: rca
  escalation_max: 3
  allowed_tools: [Read, Edit, Bash]
  max_retries: 3
  verify: python3 -m build --wheel
  with: |
    The build failed. Look at the error in the CONTEXT block, fix the
    setup.py or pyproject.toml or source file that caused it, and
    verify by trying the build again.
  next: start

lint:
  do: cmd python3 -m ruff check src/
  transitions:
    - if: fail
      goto: fix_lint
    - if: success
      goto: smoke

fix_lint:
  do: agent claude
  methodology: systematic-coverage
  escalation_max: 2
  allowed_tools: [Read, Edit, Bash]
  max_retries: 2
  verify: python3 -m ruff check src/
  with: |
    Ruff flagged lint issues in the CONTEXT block. Fix them in place.
    No style nits — only the errors ruff reported.
  next: lint

smoke:
  do: cmd python3 -c "import mypkg; mypkg.smoke_test()"
  transitions:
    - if: fail
      goto: fix_smoke
    - if: success
      goto: done

fix_smoke:
  do: agent claude
  methodology: rca
  escalation_max: 3
  allowed_tools: [Read, Edit, Bash]
  max_retries: 3
  verify: python3 -c "import mypkg; mypkg.smoke_test()"
  with: |
    The smoke test failed after build+lint passed. Find the import-time
    or startup-path bug that ruff and the type checker missed.
  next: smoke

done:
  do: cmd echo "release candidate built, linted, smoke-tested"
"""


INVESTIGATE = """\
# Example 3: multi-step investigation with state handoff (complex)
# Goal: find a P4 changelist, understand it, propose a verification plan

find_cl:
  do: agent claude
  methodology: search-first
  escalation_max: 2
  allowed_tools: [Bash, Read, Grep]
  max_retries: 2
  verify: test -n "{{state.cl_number}}"
  with: |
    Search Perforce for the changelist that adds RV32 ECC support.
    Try: p4 changes -u hren -m 50 | grep -i ecc
    Report the CL number, description, and list of changed files in
    state_updates.cl_number, state_updates.cl_description,
    state_updates.cl_files.
  next: read_cl

read_cl:
  do: agent claude
  methodology: search-first
  escalation_max: 2
  allowed_tools: [Bash, Read, Grep]
  max_retries: 2
  verify: test -n "{{state.cl_summary}}"
  with: |
    Read the files listed in CONTEXT ({{state.cl_files}}) from CL
    {{state.cl_number}}. Summarize the ECC changes: new signals,
    modified logic, encoding scheme. Emit state_updates.cl_summary.
  next: plan

plan:
  do: agent claude
  methodology: working-backwards
  escalation_max: 3
  allowed_tools: [Read, Write]
  max_retries: 2
  verify: test -f VERIFICATION_PLAN.md
  with: |
    Based on the analysis in CONTEXT ({{state.cl_summary}}), draft a
    verification plan: list the assertions that should hold, the
    formal/simulation approach, and the files needed. Write the plan
    to VERIFICATION_PLAN.md.
  next: done

done:
  do: cmd cat VERIFICATION_PLAN.md
"""


FEW_SHOT_EXAMPLES = [
    ("Calculator fix→test loop",
     "Fix all failing tests in test_calculator.py by iterating on calculator.py.",
     CALC_DEMO),
    ("Build + lint + smoke test",
     "Build a Python wheel, lint it, and run a smoke test before declaring done.",
     BUILD),
    ("P4 changelist investigation",
     "Find the RV32 ECC changelist, read the changed files, and write a verification plan.",
     INVESTIGATE),
]


def render_examples():
    """Render FEW_SHOT_EXAMPLES as a single prompt section."""
    parts = []
    for i, (title, request, yaml) in enumerate(FEW_SHOT_EXAMPLES, 1):
        parts.append(f"### Example {i}: {title}")
        parts.append(f"Request: {request}")
        parts.append("Generated workflow.yaml:")
        parts.append("```yaml")
        parts.append(yaml.rstrip())
        parts.append("```")
        parts.append("")
    return "\n".join(parts)
