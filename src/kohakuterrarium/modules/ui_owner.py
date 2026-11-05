"""The UI-owner interface: how a module talks to the dashboard UI it owns.

Any module kind may own UI contributions: a package (process-wide), or a
plugin, tool or trigger living on one creature. An owner optionally
implements::

    def ui_state(self[, context=None]) -> dict             # sync or async
    def ui_call(self, action, args[, context=None]) -> Any # sync or async
    self.ui_emit(event, data, user_id=None)               # injected push

``context`` is passed when the method accepts it. Owners raise
:class:`OwnerArgumentError` for bad input and :class:`OwnerForbiddenError`
when the caller may not do this; anything else counts as an owner failure.
``ui_emit`` is a no-op until a UI subscriber resolves the owner. Emitting
``"state"`` replaces the UI's ``state``.
"""

import inspect
from collections.abc import Callable
from typing import Any

from kohakuterrarium.errors import InvalidRequestError, KTError, NotFoundError

UI_OWNER_KINDS: tuple[str, ...] = ("package", "plugin", "tool", "trigger")
RUNTIME_OWNER_KINDS: tuple[str, ...] = ("plugin", "tool", "trigger")


class OwnerNotLoadedError(NotFoundError):
    """No live instance of the addressed owner exists in the requested scope."""


class OwnerHasNoUIError(NotFoundError):
    """The owner exists but implements neither ``ui_state`` nor ``ui_call``."""


class OwnerArgumentError(InvalidRequestError):
    """Raised by an owner for a bad action or bad arguments (HTTP 400)."""


class OwnerForbiddenError(KTError):
    """Raised by an owner when the caller may not do this (HTTP 403)."""


class OwnerFailedError(KTError):
    """An owner method raised something other than the two errors above."""


class UIOwnerMixin:
    """Gives every module a safe ``ui_emit`` before a UI publisher is bound."""

    def ui_emit(self, event: str, data: Any = None, user_id: int | None = None) -> None:
        """Push ``event`` to UI subscribers; ``user_id`` targets one L4 user."""
        publisher = self.__dict__.get("_ui_publisher")
        if publisher is not None:
            publisher(event, data, user_id)


def parse_owner(owner: str) -> tuple[str, str]:
    """Split ``"<kind>:<name>"``; raise for an unknown kind or empty name."""
    kind, sep, name = str(owner or "").partition(":")
    if not sep or kind not in UI_OWNER_KINDS or not name.strip():
        raise InvalidRequestError(
            f"owner {owner!r} must be '<kind>:<name>' with kind in {list(UI_OWNER_KINDS)}"
        )
    return kind, name.strip()


def bind_publisher(instance: Any, publisher: Callable[..., None]) -> None:
    """Route ``instance.ui_emit`` to ``publisher(event, data, user_id)``."""
    instance.__dict__["_ui_publisher"] = publisher
    if not isinstance(instance, UIOwnerMixin):
        instance.ui_emit = lambda event, data=None, user_id=None: publisher(
            event, data, user_id
        )


def has_ui(instance: Any) -> bool:
    return callable(getattr(instance, "ui_state", None)) or callable(
        getattr(instance, "ui_call", None)
    )


def accepts_context(method: Any) -> bool:
    """True when ``method`` takes a ``context`` argument (or ``**kwargs``)."""
    try:
        params = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "context" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params
    )


async def _invoke(method: Any, *args: Any, context: Any = None) -> Any:
    """Run an owner method; foreign exceptions become :class:`OwnerFailedError`."""
    kwargs = {"context": context} if accepts_context(method) else {}
    try:
        value = method(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    except (OwnerArgumentError, OwnerForbiddenError):
        raise
    except Exception as e:
        raise OwnerFailedError(f"{type(e).__name__}: {e}") from e


async def owner_state(instance: Any, context: Any = None) -> dict:
    """The owner's current UI state (``{}`` when it has only ``ui_call``)."""
    if not has_ui(instance):
        raise OwnerHasNoUIError("owner exposes no UI interface")
    getter = getattr(instance, "ui_state", None)
    if not callable(getter):
        return {}
    state = await _invoke(getter, context=context)
    return state if isinstance(state, dict) else {"value": state}


async def owner_call(
    instance: Any, action: str, args: dict | None, context: Any = None
) -> Any:
    """Run ``ui_call(action, args)``; ``context`` is passed when accepted."""
    if not has_ui(instance):
        raise OwnerHasNoUIError("owner exposes no UI interface")
    handler = getattr(instance, "ui_call", None)
    if not callable(handler):
        raise InvalidRequestError("owner accepts no UI actions")
    return await _invoke(handler, action, args or {}, context=context)


def resolve_runtime_owner(agent: Any, kind: str, name: str) -> Any:
    """The live plugin / tool / trigger called ``name`` on ``agent``, or raise."""
    instance = None
    if kind == "plugin":
        plugins = getattr(agent, "plugins", None)
        if plugins is not None and plugins.is_enabled(name):
            instance = plugins.get_plugin(name)
    elif kind == "tool":
        registry = getattr(agent, "registry", None)
        instance = registry.get_tool(name) if registry is not None else None
    elif kind == "trigger":
        manager = getattr(agent, "trigger_manager", None)
        instance = manager.get_trigger(name) if manager is not None else None
        if instance is None and manager is not None:
            instance = next(
                (
                    t
                    for t in manager.iter_triggers()
                    if getattr(t, "ui_name", "") == name
                ),
                None,
            )
    if instance is None:
        raise OwnerNotLoadedError(f"{kind}:{name} is not loaded on this creature")
    return instance
