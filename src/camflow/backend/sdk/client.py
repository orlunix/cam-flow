"""SDK API client wrapper (placeholder)."""


class SDKClient:
    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key
        self.base_url = base_url

    def query(self, prompt):
        raise NotImplementedError("SDK client not yet implemented")
