import yaml


def load_workflow(path):
    with open(path) as f:
        return yaml.safe_load(f)
