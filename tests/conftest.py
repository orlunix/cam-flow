"""Shared pytest fixtures and path setup for cam-flow tests.

The ``_block_real_camc`` autouse fixture is the global safety net
against tests accidentally shelling out to the real ``camc`` binary.
It runs in EVERY test by default, so unit + resume + integration +
error_injection suites all inherit it. Suite-specific conftests can
add stricter mocking on top, but they must NOT undo this block — the
cost of a single leaked agent is high (we self-killed once already
this way; see ``docs/triage-2026-04-26.md``).

The block is applied at the **leaf** subprocess wrappers, not at the
orchestration layer. So tests that want to exercise
``Engine._ensure_steward`` / ``spawn_steward`` / ``is_steward_alive``
in unit form keep working — they just can't actually reach ``camc``.

Tests that intentionally want to drive a real ``camc`` agent can opt
out via ``@pytest.mark.allow_real_camc`` and own the teardown.
"""

import os
import sys

import pytest

# Make `src/` importable without requiring `pip install -e .`
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture(autouse=True)
def _block_real_camc(request, monkeypatch):
    """Globally block real ``camc`` shell-outs from the engine and
    Steward paths. Block lives at the leaf wrappers so the
    orchestration layers above (``_ensure_steward``, ``spawn_steward``,
    ``is_steward_alive``) can still be unit-tested.

    Tests that intentionally want a real ``camc`` agent can opt out
    with ``@pytest.mark.allow_real_camc`` (and must clean up after
    themselves).
    """
    if request.node.get_closest_marker("allow_real_camc"):
        yield
        return

    from camflow.steward import events as events_module
    from camflow.steward import spawn as spawn_module

    # Steward spawn: real ``camc run`` would leak a tmux session in
    # ``tmp_path`` for every Engine constructed during a test.
    monkeypatch.setattr(
        spawn_module, "_default_camc_runner",
        lambda name, project_dir, prompt: "stub" + name[-8:],
    )
    # Steward liveness probe: never reach real ``camc status``.
    monkeypatch.setattr(
        spawn_module, "_camc_status", lambda agent_id: None,
    )
    # Event delivery: never reach real ``camc send``.
    monkeypatch.setattr(
        events_module, "_default_camc_send", lambda *a, **k: False,
    )

    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_real_camc: opt out of the global camc shell-out block "
        "(use only in tests that own teardown of real agents)",
    )
