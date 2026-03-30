import yaml
from runtime.daemon.daemon import run_daemon

if __name__ == "__main__":
    with open("demo/workflow_daemon.yaml") as f:
        workflow = yaml.safe_load(f)

    run_daemon(workflow)
