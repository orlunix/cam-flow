import argparse
import sys

from camflow.engine.dsl import load_workflow, validate_workflow
from camflow.backend.cam.daemon import run_daemon


def main():
    parser = argparse.ArgumentParser(
        prog="camflow",
        description="cam-flow: Lightweight stateful workflow engine for agent execution",
    )
    parser.add_argument("workflow", help="Path to workflow YAML file")
    parser.add_argument("--validate", action="store_true", help="Validate workflow and exit")
    args = parser.parse_args()

    workflow = load_workflow(args.workflow)

    valid, errors = validate_workflow(workflow)
    if not valid:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.validate:
        print("Workflow is valid.")
        sys.exit(0)

    final_state = run_daemon(workflow)
    print(f"Workflow finished: status={final_state.get('status')}, pc={final_state.get('pc')}")
    sys.exit(0 if final_state.get("status") == "done" else 1)


if __name__ == "__main__":
    main()
