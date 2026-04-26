"""Project-scoped agent registry.

Tracks every camc agent ever spawned in a camflow project (steward,
planner, workers) — alive, completed, failed, killed. The registry is
the SNAPSHOT view of "who exists?" and complements ``trace.log`` (the
TIMELINE view of "what happened?").

Public API:
    load_registry(project_dir)              -> dict
    register_agent(project_dir, agent)      -> None
    update_agent_status(project_dir, ...)   -> None
    get_agent(project_dir, agent_id)        -> dict | None
    list_agents(project_dir, role=, status=)-> list[dict]
    set_current_steward(project_dir, id)    -> None
    get_current_steward(project_dir)        -> dict | None

See ``docs/design-next-phase.md`` §12 for schema and lifecycle rules.
"""

from camflow.registry.agents import (
    REGISTRY_FILE,
    REGISTRY_VERSION,
    ROLES,
    STATUSES,
    get_agent,
    get_current_steward,
    list_agents,
    load_registry,
    register_agent,
    registry_path,
    set_current_steward,
    update_agent_status,
)
from camflow.registry.hooks import (
    on_agent_finalized,
    on_agent_handoff_archived,
    on_agent_killed,
    on_agent_spawned,
)

__all__ = [
    "REGISTRY_FILE",
    "REGISTRY_VERSION",
    "ROLES",
    "STATUSES",
    "get_agent",
    "get_current_steward",
    "list_agents",
    "load_registry",
    "on_agent_finalized",
    "on_agent_handoff_archived",
    "on_agent_killed",
    "on_agent_spawned",
    "register_agent",
    "registry_path",
    "set_current_steward",
    "update_agent_status",
]
