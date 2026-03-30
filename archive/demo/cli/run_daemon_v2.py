import yaml
from runtime.daemon.daemon_v2 import run_daemon

if __name__ == "__main__":
    with open("demo/workflow_daemon.yaml") as f:
        workflow = yaml.safe_load(f)

    trace = run_daemon(workflow)

    print("\nFinal Trace:")
    for t in trace:
        print(t)
