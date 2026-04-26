"""Error-injection fixtures.

The global Steward block lives in ``tests/conftest.py``; here we add
the camc cleanup-helper no-ops so error-injection paths don't shell
out to the real ``camc rm``.
"""

import pytest


@pytest.fixture(autouse=True)
def _mock_camc_cleanup(monkeypatch):
    from camflow.backend.cam import agent_runner

    monkeypatch.setattr(agent_runner, "_cleanup_agent", lambda *a, **k: None)
    monkeypatch.setattr(agent_runner, "_stop_agent", lambda *a, **k: None)
    monkeypatch.setattr(agent_runner, "_rm_agent", lambda *a, **k: None)
    monkeypatch.setattr(agent_runner, "_list_camflow_agent_ids", lambda: [])
    monkeypatch.setattr(
        agent_runner, "cleanup_all_camflow_agents", lambda: None,
    )
    monkeypatch.setattr(
        agent_runner, "kill_existing_camflow_agents", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        agent_runner, "cleanup_workers_of_flow", lambda *a, **k: None,
    )
