"""Define autonomous trigger protocols and shared lifecycle behavior."""

import weakref
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol, runtime_checkable

from kohakuterrarium.core.events import TriggerEvent
from kohakuterrarium.modules.ui_owner import UIOwnerMixin


def weak_reference(obj: Any) -> "weakref.ref | None":
    """Return a weak reference to ``obj``, or ``None`` if it cannot have one."""
    if obj is None:
        return None
    try:
        return weakref.ref(obj)
    except TypeError:
        return None


@runtime_checkable
class TriggerModule(Protocol):
    """Define lifecycle and event delivery for autonomous trigger modules."""

    async def start(self) -> None:
        """Start the trigger."""
        ...

    async def stop(self) -> None:
        """Stop the trigger."""
        ...

    async def wait_for_trigger(self) -> TriggerEvent | None:
        """Wait for the next event, returning none after the trigger stops."""
        ...

    def set_context(self, context: dict[str, Any]) -> None:
        """Update state consumed by context-sensitive triggers."""
        ...


class BaseTrigger(UIOwnerMixin, ABC):
    """Provide trigger lifecycle, context, setup metadata, and event creation."""

    resumable: bool = False
    # Universal triggers may be installed at runtime through a generated tool.
    universal: ClassVar[bool] = False
    # Stable name UI contributions use for this trigger (``trigger:<ui_name>``).
    ui_name: ClassVar[str] = ""

    setup_tool_name: ClassVar[str] = ""
    # The adapter marks setup descriptions as long-lived trigger side effects.
    setup_description: ClassVar[str] = ""
    # None denotes a setup tool with no arguments.
    setup_param_schema: ClassVar[dict[str, Any] | None] = None
    # Empty setup documentation falls back to the one-line description.
    setup_full_doc: ClassVar[str] = ""
    # Manual-read gating protects triggers whose constraints exceed their schema.
    setup_require_manual_read: ClassVar[bool] = False

    # Set by the trigger manager through bind_host; empty until bound.
    host_creature_id: str = ""
    host_agent_name: str = ""
    _host_agent_ref: "weakref.ref | None" = None

    def __init__(
        self,
        prompt: str | None = None,
        **options: Any,
    ):
        """Initialize the default prompt, options, context, and stopped state."""
        self.prompt = prompt
        self.options = options
        self._running = False
        self._context: dict[str, Any] = {}

    @property
    def host_agent(self) -> Any:
        """The agent hosting this trigger, or ``None`` before binding."""
        ref = self._host_agent_ref
        return ref() if ref is not None else None

    def bind_host(self, creature_id: str, agent_name: str, agent: Any) -> None:
        """Record the hosting creature; may be called again when it changes.

        ``creature_id`` is the engine creature id when hosted by a Terrarium,
        otherwise the agent's config name. ``agent`` is kept only as a weak
        reference. Overrides should call ``super().bind_host(...)``.
        """
        self.host_creature_id = creature_id
        self.host_agent_name = agent_name
        self._host_agent_ref = weak_reference(agent)

    def to_resume_dict(self) -> dict[str, Any]:
        """Serialize constructor data required to resume this trigger."""
        return {"prompt": self.prompt, **self.options}

    @classmethod
    def from_resume_dict(cls, data: dict[str, Any]) -> "BaseTrigger":
        """Recreate a trigger from persisted constructor data."""
        return cls(**data)

    @classmethod
    def from_setup_args(cls, args: dict[str, Any]) -> "BaseTrigger":
        """Build from setup arguments, defaulting to the resume-data contract."""
        return cls.from_resume_dict(args)

    @classmethod
    def post_setup(cls, trigger: "BaseTrigger", context: Any) -> None:
        """Wire context-derived state before a generated trigger is registered."""
        return None

    @property
    def is_running(self) -> bool:
        """Check if trigger is running."""
        return self._running

    async def start(self) -> None:
        """Start the trigger."""
        self._running = True
        await self._on_start()

    async def stop(self) -> None:
        """Stop the trigger."""
        self._running = False
        await self._on_stop()

    async def _on_start(self) -> None:
        """Handle subclass-specific startup."""
        pass

    async def _on_stop(self) -> None:
        """Handle subclass-specific shutdown."""
        pass

    def set_context(self, context: dict[str, Any]) -> None:
        """Update trigger context."""
        self._context.update(context)
        self._on_context_update(context)

    def _on_context_update(self, context: dict[str, Any]) -> None:
        """React to newly merged trigger context."""
        pass

    @abstractmethod
    async def wait_for_trigger(self) -> TriggerEvent | None:
        """Wait for the next trigger event."""
        ...

    def _create_event(
        self,
        event_type: str,
        content: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TriggerEvent:
        """Create a trigger event with default values."""
        return TriggerEvent(
            type=event_type,
            content=content or self.prompt or "",
            context=context or self._context.copy(),
            prompt_override=self.prompt,
        )
