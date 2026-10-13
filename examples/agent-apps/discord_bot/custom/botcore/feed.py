"""Bind a creature to one Discord channel as a persistent incremental feed.

``DiscordChannelPlugin`` is the only per-creature setting: it connects
the shared ``DiscordHub``, subscribes a ``ChannelFeed`` for its channel,
persists the delivery cursor in the session, and installs a
``DiscordFeedTrigger`` that turns buffered activity into turns. The first
delivery in a session carries a history backfill (or, after a resume, the
messages missed while offline); every later delivery carries only new
messages, edits, and deletions.
"""

import asyncio
import os
from typing import Any, Literal

import discord

from kohakuterrarium.core.events import TriggerEvent
from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.modules.trigger import BaseTrigger
from kohakuterrarium.utils.logging import get_logger

from .feed_format import (
    ImageOptions,
    build_images,
    channel_attrs,
    combine,
    render_block,
    render_item,
    render_message,
)
from .hub import HUB, ChannelFeed, FeedItem

logger = get_logger("kohakuterrarium.custom.discord_feed")

# Discord shows a typing indicator for about ten seconds per request.
TYPING_REFRESH_S = 8.0


class DiscordFeedTrigger(BaseTrigger):
    """Deliver one channel's buffered activity as ``<discord_update>`` turns."""

    def __init__(
        self,
        feed: ChannelFeed,
        *,
        backfill_limit: int = 100,
        backfill_media_limit: int = 16,
        batch_delay: float = 0.5,
        typing_indicator: bool = True,
        images: ImageOptions | None = None,
    ):
        super().__init__(prompt=None)
        self.feed = feed
        self.backfill_limit = backfill_limit
        self.backfill_media_limit = backfill_media_limit
        self.batch_delay = batch_delay
        self.typing_indicator = typing_indicator and not feed.readonly
        self.images = images or ImageOptions()
        self._typing_task: asyncio.Task | None = None

    async def _on_stop(self) -> None:
        await self._stop_typing()

    async def _typing_loop(self, channel: discord.abc.Messageable) -> None:
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(TYPING_REFRESH_S)
        except (asyncio.CancelledError, discord.DiscordException):
            pass

    async def _start_typing(self) -> None:
        if not self.typing_indicator:
            return
        channel = await self.feed.client.resolve_text_channel(self.feed.channel_id)
        if channel is not None:
            self._typing_task = asyncio.create_task(self._typing_loop(channel))

    async def _stop_typing(self) -> None:
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def wait_for_trigger(self) -> TriggerEvent | None:
        """Return the next delivery; the previous turn has ended by the time this runs."""
        await self._stop_typing()
        if not self._running:
            return None
        if not await self.feed.wait(timeout=1.0):
            return None
        # Let a burst of messages land in the same delivery.
        await asyncio.sleep(self.batch_delay)
        event = await self.compose()
        if event is not None:
            await self._start_typing()
        return event

    async def compose(self) -> TriggerEvent | None:
        """Drain the feed into one event, bootstrapping history on first use."""
        feed = self.feed
        history = None
        if not feed.bootstrapped:
            history = await feed.bootstrap(self.backfill_limit)
        items = feed.take_pending()
        if not items and not (history and history.messages):
            return None

        new_messages = [i.message for i in items if i.message is not None]
        attrs = channel_attrs(feed)
        if feed.readonly:
            attrs["mode"] = "read-only"

        blocks = []
        if history is not None and history.messages:
            backfill_attrs = dict(attrs)
            if history.kind == "resync":
                backfill_attrs["note"] = "sent while you were offline"
            else:
                backfill_attrs["note"] = "earlier history, already happened"
            if history.truncated:
                backfill_attrs["truncated"] = "older messages omitted"
            blocks.append(
                render_block(
                    "discord_backfill",
                    backfill_attrs,
                    [render_message(m) for m in history.messages],
                )
            )
        if feed.gap_truncated:
            attrs["gap"] = "some messages were missed during a reconnect"
            feed.gap_truncated = False
        if items:
            blocks.append(
                render_block("discord_update", attrs, [render_item(i) for i in items])
            )

        media_sources = (
            list(history.messages[-self.backfill_media_limit :])
            if (history and self.backfill_media_limit > 0)
            else []
        )
        media_sources += new_messages
        images = await build_images(
            [m for m in media_sources if not m.is_self], self.images
        )

        delivered = [i.message_id for i in items if i.kind == "message"]
        if history is not None:
            delivered += [m.message_id for m in history.messages]
        feed.commit(delivered)
        # A new delivery starts a new decision, so the send hold re-arms.
        feed.hold_bypass = False

        return TriggerEvent(
            type="user_input",
            content=combine("\n\n".join(blocks), images),
            context={
                "source": "discord",
                "channel_id": feed.channel_id,
                "is_mention": any(m.is_mention for m in new_messages),
                "message_count": len(new_messages),
                "bootstrap": history.kind if history is not None else None,
                "image_count": len(images),
            },
            stackable=True,
        )


def render_held(items: list[FeedItem], feed: ChannelFeed) -> str:
    """Render activity intercepted by ``send_discord`` as an update block."""
    attrs = channel_attrs(feed)
    return render_block("discord_update", attrs, [render_item(i) for i in items])


class DiscordChannelPlugin(BasePlugin):
    """Bind this creature to one Discord channel through the shared hub."""

    name = "discord_channel"
    priority = 50
    description = "Feed one Discord channel into this creature incrementally."

    def __init__(
        self,
        channel_id: int,
        token_env: str = "DISCORD_BOT_TOKEN",
        timezone: str | None = None,
        readonly: bool = False,
        backfill_limit: int = 100,
        backfill_media_limit: int = 16,
        resync_limit: int = 200,
        batch_delay: float = 0.5,
        typing_indicator: bool = True,
        hold_on_new_messages: bool = True,
        include_images: bool = True,
        image_detail: Literal["auto", "low", "high"] = "auto",
        max_images_per_message: int = 4,
        max_total_images: int = 8,
        gif_sample_frames: list[str] | None = None,
    ):
        super().__init__()
        self.channel_id = int(channel_id)
        self.token_env = token_env
        self.timezone = timezone
        self.readonly = readonly
        self.backfill_limit = backfill_limit
        self.backfill_media_limit = backfill_media_limit
        self.resync_limit = resync_limit
        self.batch_delay = batch_delay
        self.typing_indicator = typing_indicator
        self.hold_on_new_messages = hold_on_new_messages
        self.images = ImageOptions(
            enabled=include_images,
            detail=image_detail,
            max_per_message=max_images_per_message,
            max_total=max_total_images,
            gif_sample_frames=gif_sample_frames or ["first", "middle", "last"],
        )
        self._ctx: PluginContext | None = None
        self._feed: ChannelFeed | None = None
        self._agent: Any = None

    @property
    def _cursor_key(self) -> str:
        # Recipe creatures share one session store, so the key carries the channel.
        return f"cursor:{self.channel_id}"

    def _load_cursor(self) -> int | None:
        if self._ctx is None:
            return None
        value = self._ctx.get_state(self._cursor_key)
        return int(value) if value else None

    def _save_cursor(self, message_id: int) -> None:
        if self._ctx is not None:
            self._ctx.set_state(self._cursor_key, str(message_id))

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context

    async def on_agent_start(self) -> None:
        agent = self._ctx.host_agent if self._ctx is not None else None
        if agent is None:
            logger.error("discord_channel plugin has no host agent")
            return
        token = os.environ.get(self.token_env, "")
        if not token:
            logger.error(
                "Discord token missing; channel feed disabled",
                extra={"token_env": self.token_env, "channel_id": self.channel_id},
            )
            return
        try:
            client = await HUB.acquire(token, self.timezone)
        except Exception as e:
            logger.error(
                "Discord connection failed; channel feed disabled",
                extra={"channel_id": self.channel_id, "error": str(e)},
                exc_info=True,
            )
            return
        feed = ChannelFeed(
            client,
            self.channel_id,
            readonly=self.readonly,
            hold_enabled=self.hold_on_new_messages,
            resync_limit=self.resync_limit,
            load_cursor=self._load_cursor,
            save_cursor=self._save_cursor,
        )
        HUB.subscribe(feed, agent)
        self._feed, self._agent = feed, agent
        trigger = DiscordFeedTrigger(
            feed,
            backfill_limit=self.backfill_limit,
            backfill_media_limit=self.backfill_media_limit,
            batch_delay=self.batch_delay,
            typing_indicator=self.typing_indicator,
            images=self.images,
        )
        try:
            await agent.add_trigger(
                trigger, trigger_id=f"discord_feed_{self.channel_id}"
            )
        except Exception:
            await self.on_agent_stop()
            raise
        logger.info(
            "Discord channel bound",
            extra={"agent": self._ctx.agent_name, "channel_id": self.channel_id},
        )

    async def on_agent_stop(self) -> None:
        feed, agent = self._feed, self._agent
        self._feed, self._agent = None, None
        if feed is None:
            return
        HUB.unsubscribe(feed, agent)
        await HUB.release()
