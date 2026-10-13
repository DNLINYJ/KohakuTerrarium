"""Share one Discord connection across per-channel creatures in a process.

``DiscordHub`` owns a single gateway client, reference-counted by the
creatures that use it. Each creature subscribes a ``ChannelFeed``: the
per-channel buffer of live activity (new messages, edits, deletes) plus
the delivery cursor that decides what the creature has already seen.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import discord

from kohakuterrarium.utils.logging import get_logger

from .client import DiscordClient, register_client, unregister_client
from .records import DiscordMessage

logger = get_logger("kohakuterrarium.custom.discord_hub")

# Bounds the per-feed memory of seen messages used for dedup and edit diffs.
KNOWN_MESSAGES_LIMIT = 2000
READY_TIMEOUT_S = 60.0


@dataclass
class FeedItem:
    """One unit of channel activity waiting to be shown to the creature."""

    kind: Literal["message", "edit", "delete"]
    message_id: int
    author: str = ""
    content: str = ""
    old_content: str | None = None
    message: DiscordMessage | None = None


@dataclass
class Bootstrap:
    """Result of a feed's first read in a session."""

    kind: Literal["backfill", "resync"]
    messages: list[DiscordMessage]
    truncated: bool


class ChannelFeed:
    """Buffer one channel's live activity and track what has been delivered.

    The cursor is the highest message id already delivered to the creature;
    it is loaded from and saved to the creature's session via callbacks so a
    resumed session catches up instead of reading history again.
    """

    def __init__(
        self,
        client: DiscordClient,
        channel_id: int,
        *,
        readonly: bool = False,
        hold_enabled: bool = True,
        resync_limit: int = 200,
        load_cursor: Callable[[], int | None] | None = None,
        save_cursor: Callable[[int], None] | None = None,
    ):
        self.client = client
        self.channel_id = channel_id
        self.readonly = readonly
        self.hold_enabled = hold_enabled
        self.resync_limit = resync_limit
        self._load_cursor = load_cursor
        self._save_cursor = save_cursor

        self.channel_name = ""
        self.guild_name = ""
        self.cursor: int | None = None
        self.bootstrapped = False
        self.hold_bypass = False
        self.gap_truncated = False
        self._pending: list[FeedItem] = []
        self._known: OrderedDict[int, tuple[str, str]] = OrderedDict()
        self._wakeup = asyncio.Event()

    @property
    def has_pending(self) -> bool:
        """Return whether undelivered activity is buffered."""
        return bool(self._pending)

    def _remember(self, message_id: int, author: str, content: str) -> None:
        """Record a seen message for dedup and edit comparison."""
        self._known[message_id] = (author, content)
        self._known.move_to_end(message_id)
        while len(self._known) > KNOWN_MESSAGES_LIMIT:
            self._known.popitem(last=False)

    def _learn_names(self, msg: DiscordMessage) -> None:
        self.channel_name = msg.channel_name or self.channel_name
        self.guild_name = msg.guild_name or self.guild_name

    def _push(self, item: FeedItem) -> None:
        self._pending.append(item)
        self._wakeup.set()

    def _pending_message(self, message_id: int) -> FeedItem | None:
        for item in self._pending:
            if item.kind == "message" and item.message_id == message_id:
                return item
        return None

    def on_discord_message(self, msg: DiscordMessage) -> None:
        """Buffer a new message from this channel unless already seen."""
        if msg.channel_id != self.channel_id or msg.message_id in self._known:
            return
        self._remember(msg.message_id, msg.author_display_name, msg.content)
        self._learn_names(msg)
        self._push(
            FeedItem(
                kind="message",
                message_id=msg.message_id,
                author=msg.author_display_name,
                content=msg.content,
                message=msg,
            )
        )

    def on_discord_edit(self, message: discord.Message, old: str | None) -> None:
        """Fold an edit into an undelivered message, or buffer it as an edit."""
        if message.channel.id != self.channel_id:
            return
        content = self.client.parse_mentions(message.content, message.guild)
        author = message.author.display_name
        pending = self._pending_message(message.id)
        if pending is not None and pending.message is not None:
            pending.message.content = content
            pending.content = content
            self._remember(message.id, author, content)
            return
        known = self._known.get(message.id)
        previous = known[1] if known is not None else old
        if previous is not None and previous == content:
            return
        self._remember(message.id, author, content)
        self._push(
            FeedItem(
                kind="edit",
                message_id=message.id,
                author=author,
                content=content,
                old_content=previous,
            )
        )

    def on_discord_delete(self, channel_id: int, message_ids: list[int]) -> None:
        """Drop undelivered messages; report deletions of delivered ones."""
        if channel_id != self.channel_id:
            return
        for message_id in message_ids:
            if self._pending_message(message_id) is not None:
                self._pending = [i for i in self._pending if i.message_id != message_id]
                continue
            known = self._known.get(message_id)
            if known is None:
                continue
            self._push(FeedItem(kind="delete", message_id=message_id, author=known[0]))

    async def on_discord_reconnect(self) -> None:
        """Fetch messages missed while the gateway session was replaced."""
        if not self.bootstrapped:
            return
        last_seen = max(self._known, default=self.cursor)
        if last_seen is None:
            return
        channel = await self.client.resolve_text_channel(self.channel_id)
        if channel is None:
            return
        missed = await self.client.fetch_messages(
            channel, limit=self.resync_limit, after=last_seen
        )
        if len(missed) >= self.resync_limit:
            self.gap_truncated = True
        for msg in missed:
            if not msg.is_self:
                self.on_discord_message(msg)
        logger.info(
            "Reconnect gap filled",
            extra={"channel_id": self.channel_id, "missed": len(missed)},
        )

    async def wait(self, timeout: float) -> bool:
        """Wait until activity is buffered; False on timeout."""
        if self._pending:
            return True
        self._wakeup.clear()
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self._pending)

    async def bootstrap(self, backfill_limit: int) -> Bootstrap:
        """Read history once per session: full backfill, or a resync after resume."""
        self.bootstrapped = True
        if self.cursor is None and self._load_cursor is not None:
            self.cursor = self._load_cursor()
        kind: Literal["backfill", "resync"] = "backfill"
        after = None
        limit = backfill_limit
        if self.cursor is not None:
            kind, after, limit = "resync", self.cursor, self.resync_limit

        channel = await self.client.resolve_text_channel(self.channel_id)
        fetched: list[DiscordMessage] = []
        if channel is not None:
            fetched = await self.client.fetch_messages(
                channel, limit=limit, after=after
            )
        truncated = len(fetched) >= limit

        history = [m for m in fetched if m.message_id not in self._known]
        for msg in history:
            self._remember(msg.message_id, msg.author_display_name, msg.content)
            self._learn_names(msg)
        logger.info(
            "Feed bootstrapped",
            extra={
                "channel_id": self.channel_id,
                "kind": kind,
                "messages": len(history),
                "truncated": truncated,
            },
        )
        return Bootstrap(kind=kind, messages=history, truncated=truncated)

    def take_pending(self) -> list[FeedItem]:
        """Remove and return all buffered activity."""
        items, self._pending = self._pending, []
        self._wakeup.clear()
        return items

    def commit(self, message_ids: list[int]) -> None:
        """Advance and persist the delivery cursor past ``message_ids``."""
        if not message_ids:
            return
        newest = max(message_ids)
        if self.cursor is not None and newest <= self.cursor:
            return
        self.cursor = newest
        if self._save_cursor is not None:
            self._save_cursor(newest)


class DiscordHub:
    """Own the process-wide Discord client shared by every channel feed."""

    def __init__(self) -> None:
        self.client: DiscordClient | None = None
        self._token: str | None = None
        self._refs = 0
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._feeds: list[ChannelFeed] = []
        self._agent_feeds: dict[int, ChannelFeed] = {}

    async def acquire(self, token: str, timezone: str | None = None) -> DiscordClient:
        """Return the connected client, logging in on first use."""
        async with self._lock:
            if self.client is not None:
                if token != self._token:
                    raise ValueError(
                        "DiscordHub already connected with a different bot token"
                    )
                self._refs += 1
                return self.client

            client = DiscordClient(
                channel_ids=[],
                timezone=timezone,
                enqueue_messages=False,
                recent_cache_size=500,
            )
            try:
                await client.login(token)
            except BaseException:
                await client.close()
                raise
            connect_task = asyncio.create_task(client.connect())
            ready_task = asyncio.create_task(client.wait_until_ready())
            done, _ = await asyncio.wait(
                {connect_task, ready_task},
                timeout=READY_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_task not in done:
                ready_task.cancel()
                await client.close()
                if not connect_task.done():
                    connect_task.cancel()
                    raise RuntimeError(
                        f"Discord client not ready after {READY_TIMEOUT_S:.0f}s"
                    )
                exc = connect_task.exception()
                raise RuntimeError(f"Discord connection ended before ready: {exc}")

            self.client = client
            self._token = token
            self._connect_task = connect_task
            self._refs = 1
            register_client("default", client)
            logger.info("Discord hub connected", extra={"bot": str(client.user)})
            return client

    async def release(self) -> None:
        """Drop one reference; close the client when the last user leaves."""
        async with self._lock:
            if self.client is None:
                return
            self._refs -= 1
            if self._refs > 0:
                return
            client, task = self.client, self._connect_task
            self.client = None
            self._token = None
            self._connect_task = None
            unregister_client("default", client)
            await client.close()
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, discord.DiscordException):
                    pass
            logger.info("Discord hub disconnected")

    def subscribe(self, feed: ChannelFeed, agent: Any) -> None:
        """Route ``feed``'s channel to it and bind it to ``agent``."""
        self._feeds.append(feed)
        self._agent_feeds[id(agent)] = feed
        feed.client.add_listener(feed)
        self._sync_channels(feed.client)

    def unsubscribe(self, feed: ChannelFeed, agent: Any) -> None:
        """Stop routing to ``feed`` and drop its agent binding."""
        if feed in self._feeds:
            self._feeds.remove(feed)
        if self._agent_feeds.get(id(agent)) is feed:
            del self._agent_feeds[id(agent)]
        feed.client.remove_listener(feed)
        self._sync_channels(feed.client)

    def feed_for_agent(self, agent: Any) -> ChannelFeed | None:
        """Return the feed bound to a creature's agent, if any."""
        if agent is None:
            return None
        return self._agent_feeds.get(id(agent))

    def _sync_channels(self, client: DiscordClient) -> None:
        """Limit gateway handling to channels some feed subscribes to."""
        writable = {f.channel_id for f in self._feeds if not f.readonly}
        readonly = {f.channel_id for f in self._feeds if f.readonly} - writable
        # An empty set would mean "every channel", so keep a never-matching id.
        client.channel_ids = writable or {0}
        client.readonly_channel_ids = readonly


HUB = DiscordHub()
