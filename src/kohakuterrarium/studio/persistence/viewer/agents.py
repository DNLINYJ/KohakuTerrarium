"""Key-only viewer agent enumeration shared by summary and history index.

Unions metadata agents with attached namespaces and optionally prepends
``viewer_default_agent``. Callers must pass an already-open store; this
module never opens a database or builds a summary payload.
"""

from typing import Any

from kohakuterrarium.errors import NotFoundError
from kohakuterrarium.session.store import SessionStore


def viewer_agent_names(
    meta: dict[str, Any], store: SessionStore, requested: str | None = None
) -> list[str]:
    """Select and order agent namespaces for unfiltered viewer discovery.

    A requested agent must belong to the session. Without one, all main and
    attached namespaces are included, with ``viewer_default_agent`` first.
    ``load_meta`` already includes event-discovered creatures in ``agents``.
    """
    main_agents = list(meta.get("agents") or [])
    attached_namespaces = [
        e["namespace"] for e in store.discover_attached_agents() if e.get("namespace")
    ]
    known_agents = main_agents + [
        n for n in attached_namespaces if n not in main_agents
    ]
    if requested is None:
        default = meta.get("viewer_default_agent")
        if isinstance(default, str) and default in known_agents:
            return [default] + [a for a in known_agents if a != default]
        return known_agents
    if requested not in known_agents:
        raise NotFoundError(f"Agent not found in session: {requested}")
    return [requested]
