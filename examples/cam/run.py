"""Example: run a workflow using the CAM (Coding Agent Manager) backend."""

from camflow.engine.dsl import load_workflow
from camflow.backend.cam.daemon import run_daemon


def main():
    workflow = load_workflow("workflow.yaml")
    final_state = run_daemon(workflow)
    print(f"Done: {final_state}")


if __name__ == "__main__":
    main()
