"""Example: run a workflow using the SDK backend (placeholder)."""

from camflow.engine.dsl import load_workflow
from camflow.backend.sdk.client import SDKClient
from camflow.backend.sdk.executor import SDKBackend


def main():
    workflow = load_workflow("../cam/workflow.yaml")

    # placeholder — replace with real API client
    client = SDKClient(api_key="your-api-key")
    backend = SDKBackend(client)

    # SDK backend executes one node at a time
    # you control the loop programmatically
    node_id = "start"
    state = {"pc": node_id, "status": "running"}

    try:
        result = backend.execute_node(node_id, workflow[node_id], state)
        print(f"Result: {result}")
    except NotImplementedError:
        print("SDK client not yet implemented — this is a placeholder example.")


if __name__ == "__main__":
    main()
