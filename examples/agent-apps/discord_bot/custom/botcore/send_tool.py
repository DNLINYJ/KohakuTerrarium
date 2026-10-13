"""Expose guarded Discord output through native tool calling.

Wraps the Discord output policy (keyword filtering, dedup, reply and
mention markers) as a tool. A creature bound to a channel by
``DiscordChannelPlugin`` always sends to that channel; otherwise the
channel of the last batched input is used.
"""

from typing import Any

from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.utils.logging import get_logger

from .feed import render_held
from .hub import HUB
from .output import DiscordOutputModule

logger = get_logger("kohakuterrarium.custom.send_discord")


class SendDiscordTool(BaseTool):
    """Send messages through the shared output policy and Discord client.

    Supports special markers in message content:
    - [reply:m123456] or [reply:Username] - reply to a message
    - [@Username] - mention a user
    """

    is_concurrency_safe = False
    needs_context = True

    def __init__(
        self,
        client_name: str = "default",
        filtered_keywords: list[str] | None = None,
        keywords_file: str | None = None,
        drop_base_chance: float = 0.25,
        drop_increment: float = 0.15,
        drop_max_chance: float = 0.7,
        dedup_threshold: float = 0.85,
        dedup_window: int = 5,
        config: ToolConfig | None = None,
    ):
        super().__init__(config)
        self._output = DiscordOutputModule(
            client_name=client_name,
            filtered_keywords=filtered_keywords,
            keywords_file=keywords_file,
            drop_base_chance=drop_base_chance,
            drop_increment=drop_increment,
            drop_max_chance=drop_max_chance,
            dedup_threshold=dedup_threshold,
            dedup_window=dedup_window,
        )

    @property
    def tool_name(self) -> str:
        return "send_discord"

    @property
    def description(self) -> str:
        return "Send a message to the current Discord channel"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        """Describe the message argument for native function calling."""
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message content to send to Discord",
                },
            },
            "required": ["message"],
        }

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Send one message, or hand back chat that arrived since the last delivery."""
        message = args.get("message", args.get("content", "")).strip()
        if not message:
            return ToolResult(error="Empty message - nothing to send")

        feed = HUB.feed_for_agent(context.agent if context else None)
        if feed is None:
            result = await self._output.send(message)
            return self._report(result)

        if feed.readonly:
            return ToolResult(error="NOT SENT: this channel is read-only for you")

        # Unseen chat may change the right reply; show it once before sending.
        if (
            feed.hold_enabled
            and feed.bootstrapped
            and feed.has_pending
            and not feed.hold_bypass
        ):
            items = feed.take_pending()
            feed.commit([i.message_id for i in items if i.kind == "message"])
            feed.hold_bypass = True
            return ToolResult(
                output=(
                    "NOT SENT: new chat arrived while you were thinking.\n"
                    f"{render_held(items, feed)}\n"
                    "Call send_discord again to send (as is or revised), "
                    "or stop if it no longer fits."
                ),
                exit_code=0,
            )

        feed.hold_bypass = False
        result = await self._output.send(
            message, channel_id=feed.channel_id, allow_drop=False
        )
        return self._report(result)

    @staticmethod
    def _report(result) -> ToolResult:
        if result.sent:
            return ToolResult(output=result.describe(), exit_code=0)
        logger.info("send_discord not sent", extra={"reason": result.reason})
        return ToolResult(output=result.describe(), exit_code=1)

    def get_full_documentation(self) -> str:
        return """# send_discord

Send a message to the current Discord channel.

## Arguments

| Arg | Type | Description |
|-----|------|-------------|
| message | string | Message content to send |

## Special Markers

Include these in your message content for special behavior:

- `[reply:m123456]` - Reply to the message with that tag
- `[reply:Username]` - Reply to the user's latest message
- `[@Username]` - Mention/ping a user

## Behavior

- The result says `Sent as m123456`, or `NOT SENT: <reason>`
- If new chat arrived since you last read, the first send returns it
  instead of sending; call again to send anyway or revise
- Near-duplicate messages within a short window are not sent
- Filtered keywords are replaced with [filtered] (reported in the result)

## Tips

- Split long responses into multiple send_discord calls (1-2 lines each)
- Use reply markers to respond to specific messages
"""
