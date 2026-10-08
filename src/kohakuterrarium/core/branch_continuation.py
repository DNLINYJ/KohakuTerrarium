"""Continue a user turn on an explicitly selected persisted branch.

Idle text continuation reloads the in-memory conversation from the event
log when the requested view differs from the live path, then enqueues the
new user input. Branch selection is persisted so resume restores it.
"""

from copy import deepcopy
from typing import Any

from kohakuterrarium.errors import ConflictError
from kohakuterrarium.session.history import (
    InvalidBranchViewError,
    resolve_branch_view_strict,
)

BRANCH_VIEW_STATE_SUFFIX = "branch_view"


def branch_view_state_key(namespace: str) -> str:
    """Return the session-state key that stores the selected branch view."""
    return f"{namespace}:{BRANCH_VIEW_STATE_SUFFIX}"


def normalize_branch_view(value: Any) -> dict[int, int] | None:
    """Detach positive turn and branch coordinates supplied by a transport."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("branch_view must be a turn-to-branch mapping")
    result: dict[int, int] = {}
    for key, branch in value.items():
        if isinstance(key, str) and key.isascii() and key.isdecimal():
            key = int(key)
        if type(key) is not int or key <= 0 or type(branch) is not int or branch <= 0:
            raise ValueError("branch_view requires positive integer coordinates")
        if key in result:
            raise ValueError("branch_view contains duplicate turn coordinates")
        result[key] = branch
    return result


def encode_branch_view(view: dict[int, int] | None) -> dict[str, int] | None:
    """JSON-safe encoding of a selected branch view."""
    if not view:
        return None
    return {str(turn): branch for turn, branch in sorted(view.items())}


def load_persisted_branch_view(store: Any, namespace: str) -> dict[int, int] | None:
    """Read a previously persisted branch view, or None when absent."""
    if store is None:
        return None
    try:
        raw = store.state.get(branch_view_state_key(namespace))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return normalize_branch_view(raw)
    except ValueError:
        return None


def persist_branch_view(store: Any, namespace: str, view: dict[int, int] | None) -> None:
    """Write the selected view into session state, or drop it when empty."""
    if store is None:
        return
    key = branch_view_state_key(namespace)
    encoded = encode_branch_view(view)
    try:
        if encoded is None:
            store.state.pop(key, None)
        else:
            store.state[key] = encoded
    except Exception:
        return


def live_branch_path(agent: Any) -> dict[int, int]:
    """Return the agent's current parent path plus the live turn/branch."""
    current: dict[int, int] = {}
    for pair in getattr(agent, "_parent_branch_path", ()) or ():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        turn, branch = pair
        if type(turn) is int and turn > 0 and type(branch) is int and branch > 0:
            current[turn] = branch
    turn = getattr(agent, "_turn_index", 0)
    branch = getattr(agent, "_branch_id", 0)
    if type(turn) is int and turn > 0 and type(branch) is int and branch > 0:
        current[turn] = branch
    return current


def selected_branch_view(agent: Any) -> dict[int, int]:
    """Return the persisted or live branch view for history / attach payloads."""
    store = getattr(agent, "session_store", None)
    namespace = getattr(getattr(agent, "config", None), "name", "") or ""
    persisted = load_persisted_branch_view(store, namespace)
    if persisted:
        return persisted
    return live_branch_path(agent)


def _events_for(agent: Any) -> list[dict[str, Any]]:
    store = getattr(agent, "session_store", None)
    if store is None:
        return []
    namespace = agent.config.name
    try:
        return list(store.get_events(namespace))
    except Exception:
        return []


async def continue_on_branch(agent: Any, event: Any, view: dict[int, int]) -> bool:
    """Select a persisted subtree when needed, then enqueue the user event."""
    if getattr(agent, "_paused", False):
        raise ConflictError("Cannot continue a selected branch while paused")

    view = deepcopy(view)
    current = live_branch_path(agent)
    store = getattr(agent, "session_store", None)
    namespace = getattr(getattr(agent, "config", None), "name", "") or ""
    if store is None:
        same = all(current.get(turn) == branch for turn, branch in view.items())
        resolved = dict(current)
        resolved.update(view)
    else:
        events = _events_for(agent)
        try:
            resolved = resolve_branch_view_strict(events, view)
        except InvalidBranchViewError as exc:
            raise ValueError(str(exc)) from exc
        same = resolved == current
        if not same:
            if getattr(agent, "is_processing", False):
                raise ConflictError(
                    "Cannot continue a selected branch while a turn is active"
                )
            reload = getattr(agent, "_reload_conversation_under_branch_view", None)
            if reload is None:
                raise ConflictError(
                    "Cannot continue a selected branch without history reload"
                )
            reload(resolved)
    persist_branch_view(store, namespace, resolved)
    return await agent._process_event(event)
