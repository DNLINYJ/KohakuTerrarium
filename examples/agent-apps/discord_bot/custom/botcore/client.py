"""Bridge discord.py messages and media into shared Terrarium I/O state.

This module contains the core Discord client that bridges discord.py
with the KohakuTerrarium framework. Message and media records live in
``discord_types`` and are re-exported here for existing imports.
"""

import asyncio
import logging
import re
from collections import deque
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import discord

from kohakuterrarium.utils.logging import get_logger

from .records import (
    MSG_TAG_PATTERN,
    CustomEmoji,
    DiscordMessage,
    MediaAttachment,
    MediaSticker,
    RecentMessage,
    msg_tag,
    parse_custom_emojis,
    short_id,
)

logger = get_logger("kohakuterrarium.custom.discord_client")

__all__ = [
    "CustomEmoji",
    "DiscordClient",
    "DiscordListener",
    "DiscordMessage",
    "MediaAttachment",
    "MediaSticker",
    "RecentMessage",
    "get_client",
    "msg_tag",
    "parse_custom_emojis",
    "register_client",
    "short_id",
]


# Custom modules are instantiated independently, so the registry shares one connection.
_shared_clients: dict[str, "DiscordClient"] = {}


def register_client(name: str, client: "DiscordClient") -> None:
    """Register a client for use by independently loaded custom modules."""
    _shared_clients[name] = client


def get_client(name: str) -> "DiscordClient | None":
    """Return the shared client registered under a module name."""
    return _shared_clients.get(name)


def unregister_client(name: str, client: "DiscordClient") -> None:
    """Remove a registry entry only if it still points at ``client``."""
    if _shared_clients.get(name) is client:
        del _shared_clients[name]


class _DiscordLogBridge(logging.Handler):
    """Forward discord.py's own warnings and errors into the kt log."""

    def emit(self, record: logging.LogRecord) -> None:
        logger.log(
            record.levelno,
            f"[{record.name}] {record.getMessage()}",
            exc_info=record.exc_info,
        )


def bridge_discord_logging() -> None:
    """Install the discord.py -> kt log bridge once per process."""
    discord_logger = logging.getLogger("discord")
    if any(isinstance(h, _DiscordLogBridge) for h in discord_logger.handlers):
        return
    bridge = _DiscordLogBridge(level=logging.WARNING)
    discord_logger.addHandler(bridge)
    if discord_logger.level == logging.NOTSET or discord_logger.level > logging.WARNING:
        discord_logger.setLevel(logging.WARNING)


# User mentions may include Discord's optional nickname marker: <@id> or <@!id>.
DISCORD_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")


class DiscordListener(Protocol):
    """Receiver of live gateway activity fanned out by ``DiscordClient``."""

    def on_discord_message(self, msg: DiscordMessage) -> None: ...

    def on_discord_edit(self, message: discord.Message, old: str | None) -> None: ...

    def on_discord_delete(self, channel_id: int, message_ids: list[int]) -> None: ...

    async def on_discord_reconnect(self) -> None: ...


class DiscordClient(discord.Client):
    """Bridge Discord I/O while retaining lookup and reply context."""

    def __init__(
        self,
        channel_ids: list[int] | None = None,
        readonly_channel_ids: list[int] | None = None,
        history_limit: int = 20,
        timezone: str | None = None,
        enqueue_messages: bool = True,
        recent_cache_size: int | None = None,
        **kwargs: Any,
    ):
        """Initialize channel policy, bounded history, and lookup caches.

        Args:
            channel_ids: List of channel IDs to listen to
            readonly_channel_ids: List of read-only channel IDs
            history_limit: Number of messages to fetch for history
            timezone: Timezone name for timestamp formatting (e.g., "Asia/Tokyo",
                     "America/New_York", "UTC"). If None, uses system local timezone.
            enqueue_messages: Feed incoming messages to the batch input queue;
                     disabled when only listeners consume them
            recent_cache_size: Per-channel reference cache size
                     (default: max(50, history_limit))
        """
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents, **kwargs)
        bridge_discord_logging()

        self.channel_ids = set(channel_ids) if channel_ids else None
        self.readonly_channel_ids = (
            set(readonly_channel_ids) if readonly_channel_ids else set()
        )
        self.history_limit = history_limit
        self.enqueue_messages = enqueue_messages
        self._message_queue: asyncio.Queue[DiscordMessage] = asyncio.Queue()

        if timezone:
            try:
                self._timezone = ZoneInfo(timezone)
            except KeyError:
                logger.warning(
                    f"Unknown timezone '{timezone}', using system local timezone"
                )
                self._timezone = None  # astimezone() then uses the system zone.
        else:
            self._timezone = None  # astimezone() then uses the system zone.

        # Output follows the channel of the most recently consumed input.
        self._current_channel_id: int | None = None

        # Per-channel buffers resolve compact reply references without API calls.
        self._recent_messages: dict[int, deque[RecentMessage]] = {}
        self._max_recent = recent_cache_size or max(50, history_limit)

        # Cache both account and display names for mention generation.
        self._user_cache: dict[str, int] = {}

        # Record channels whose history has been fetched for coordinating callers.
        self._history_fetched: set[int] = set()
        # Chronological ids of the last snapshot fetch; ``#N`` indexes into it.
        self._history_ids: dict[int, list[int]] = {}

        self._feed_listeners: list[DiscordListener] = []
        self._ready_count = 0
        self._feed_listener_tasks: set[asyncio.Task] = set()

    def add_listener(self, listener: DiscordListener) -> None:
        """Register a receiver for live messages, edits, deletes, and reconnects."""
        if listener not in self._feed_listeners:
            self._feed_listeners.append(listener)

    def remove_listener(self, listener: DiscordListener) -> None:
        """Stop delivering live activity to ``listener``."""
        if listener in self._feed_listeners:
            self._feed_listeners.remove(listener)

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """Report exceptions raised inside event handlers through the kt log."""
        logger.error(
            "Discord event handler failed",
            extra={"event": event_method},
            exc_info=True,
        )

    def _is_watched(self, channel_id: int) -> bool:
        """Return whether channel policy admits activity from ``channel_id``."""
        all_channels = set()
        if self.channel_ids:
            all_channels.update(self.channel_ids)
        all_channels.update(self.readonly_channel_ids)
        return not all_channels or channel_id in all_channels

    async def on_ready(self) -> None:
        """Log the identity and tell listeners when a later session replaced one."""
        self._ready_count += 1
        logger.info(
            "Discord client ready",
            extra={
                "bot_user": str(self.user),
                "guilds": len(self.guilds),
                "ready_count": self._ready_count,
            },
        )
        # A repeated READY means the gateway session was replaced and events were lost.
        if self._ready_count > 1:
            for listener in list(self._feed_listeners):
                task = asyncio.create_task(listener.on_discord_reconnect())
                self._feed_listener_tasks.add(task)
                task.add_done_callback(self._feed_listener_tasks.discard)

    def _format_timestamp(self, dt: datetime) -> str:
        """Format a Discord timestamp in the configured or system timezone.

        Discord returns UTC timezone-aware datetimes. This method converts
        to the configured timezone (or system local if not configured).

        Args:
            dt: UTC datetime from Discord

        Returns:
            Formatted timestamp string in "YYYY-MM-DD HH:MM" format
        """
        if self._timezone:
            local_dt = dt.astimezone(self._timezone)
        else:
            local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")

    def get_bot_identity(self) -> str:
        """Return a compact bot identity suitable for prompt context."""
        if self.user:
            return f"{self.user.display_name}({short_id(self.user.id)})"
        return "Bot(unknown)"

    def _entry(self, message: discord.Message) -> RecentMessage:
        """Cache the author's names and build a reference entry."""
        self._user_cache[message.author.display_name.lower()] = message.author.id
        self._user_cache[message.author.name.lower()] = message.author.id
        return RecentMessage(
            message_id=message.id,
            short_id=short_id(message.id),
            author_name=message.author.display_name,
            author_id=message.author.id,
            content_preview=message.content[:100] if message.content else "",
            is_bot=message.author.bot,
        )

    def remember_message(self, message: discord.Message) -> None:
        """Record one live message (always the newest) in the reference cache."""
        entry = self._entry(message)
        channel_id = message.channel.id
        if channel_id not in self._recent_messages:
            self._recent_messages[channel_id] = deque(maxlen=self._max_recent)
        recent = self._recent_messages[channel_id]
        if not any(r.message_id == message.id for r in recent):
            recent.append(entry)

    def remember_history(self, channel_id: int, messages: list[discord.Message]):
        """Merge fetched messages into the cache, keeping it chronological.

        Snowflake ids sort chronologically, so the merged cache keeps the
        newest ``recent_cache_size`` entries regardless of fetch order.
        """
        merged = {r.message_id: r for r in self._recent_messages.get(channel_id, ())}
        for msg in messages:
            merged.setdefault(msg.id, self._entry(msg))
        ordered = [merged[k] for k in sorted(merged)]
        self._recent_messages[channel_id] = deque(ordered, maxlen=self._max_recent)

    def _lookup_recent(self, channel_id: int, message_id: int) -> RecentMessage | None:
        """Return the cached reference entry for a message, if any."""
        for recent in self._recent_messages.get(channel_id, ()):
            if recent.message_id == message_id:
                return recent
        return None

    async def fetch_channel_history(
        self,
        channel: discord.TextChannel | discord.Thread,
        force: bool = False,
    ) -> list[str]:
        """Fetch and format recent channel history while warming lookup caches."""
        if self.history_limit <= 0:
            return []

        channel_id = channel.id
        self._history_fetched.add(channel_id)

        try:
            messages = []
            ids: list[int] = []
            raw: list[discord.Message] = []
            async for msg in channel.history(limit=self.history_limit):
                raw.append(msg)
                ids.append(msg.id)

                is_self = msg.author == self.user
                msg_time = self._format_timestamp(msg.created_at)
                parsed_content = self.parse_mentions(msg.content, msg.guild)
                self_marker = "[SELF] " if is_self else ""
                bot_marker = "[BOT] " if msg.author.bot and not is_self else ""
                formatted = f"[{msg_time}] {self_marker}{bot_marker}[{msg.author.display_name}]: {parsed_content}"
                messages.append(formatted)

            messages.reverse()
            ids.reverse()
            self._history_ids[channel_id] = ids
            self.remember_history(channel_id, raw)

            logger.info(
                "Fetched channel history",
                extra={"channel_id": channel_id, "message_count": len(messages)},
            )
            return messages

        except discord.DiscordException as e:
            logger.warning(
                "Failed to fetch history",
                extra={"channel_id": channel_id, "error": str(e)},
            )
            return []

    async def fetch_messages(
        self,
        channel: discord.TextChannel | discord.Thread,
        limit: int,
        after: int | None = None,
    ) -> list[DiscordMessage]:
        """Fetch up to ``limit`` newest messages (optionally after an id), oldest first.

        Every message, including the bot's own, is normalized with media.
        Replies are resolved from the fetched batch and caches only, so a
        large backfill costs one paginated history call and nothing more.
        """
        if limit <= 0:
            return []
        after_obj = discord.Object(id=after) if after else None
        raw: list[discord.Message] = []
        try:
            async for msg in channel.history(
                limit=limit, after=after_obj, oldest_first=False
            ):
                raw.append(msg)
        except discord.DiscordException as e:
            logger.warning(
                "Failed to fetch messages",
                extra={"channel_id": channel.id, "error": str(e)},
            )
            return []
        raw.reverse()
        self.remember_history(channel.id, raw)
        return [await self.build_message(msg, fetch_reply=False) for msg in raw]

    async def fetch_channel_history_with_media(
        self,
        channel: discord.TextChannel | discord.Thread,
        limit: int | None = None,
    ) -> list[DiscordMessage]:
        """Fetch recent media-bearing messages for multimodal context.

        Args:
            channel: Discord channel to fetch from
            limit: Number of messages to fetch (defaults to history_limit)

        Returns:
            List of DiscordMessage objects with media info populated
        """
        fetch_limit = limit or self.history_limit
        if fetch_limit <= 0:
            return []

        try:
            messages: list[DiscordMessage] = []
            async for msg in channel.history(limit=fetch_limit):
                if msg.author == self.user:
                    continue
                # Text-only history is supplied separately and must not be duplicated.
                if not (
                    msg.attachments or msg.stickers or parse_custom_emojis(msg.content)
                ):
                    continue
                messages.append(await self.build_message(msg, fetch_reply=False))

            messages.reverse()
            logger.debug(
                "Fetched history with media",
                extra={"channel_id": channel.id, "media_messages": len(messages)},
            )
            return messages

        except discord.DiscordException as e:
            logger.warning(
                "Failed to fetch history with media",
                extra={"channel_id": channel.id, "error": str(e)},
            )
            return []

    async def build_message(
        self, message: discord.Message, *, fetch_reply: bool = True
    ) -> DiscordMessage:
        """Normalize a discord.py message, resolving its reply target if possible.

        Reply lookup tries Discord's cache, then the local reference cache,
        then (only when ``fetch_reply``) one API call.
        """
        channel_id = message.channel.id
        reply_to_id = None
        reply_to_author = None
        reply_to_content = None
        reply_to_is_bot = False
        if message.reference and message.reference.message_id:
            reply_to_id = message.reference.message_id
            if message.reference.cached_message:
                ref_msg = message.reference.cached_message
                reply_to_author = ref_msg.author.display_name
                reply_to_content = self.parse_mentions(ref_msg.content, message.guild)
                reply_to_is_bot = ref_msg.author.bot
            else:
                recent = self._lookup_recent(channel_id, reply_to_id)
                if recent is not None:
                    reply_to_author = recent.author_name
                    reply_to_content = recent.content_preview
                    reply_to_is_bot = recent.is_bot
                elif fetch_reply:
                    try:
                        ref_msg = await message.channel.fetch_message(reply_to_id)
                        reply_to_author = ref_msg.author.display_name
                        reply_to_content = self.parse_mentions(
                            ref_msg.content[:100] if ref_msg.content else "",
                            message.guild,
                        )
                        reply_to_is_bot = ref_msg.author.bot
                    except discord.DiscordException:
                        pass

        return DiscordMessage(
            content=self.parse_mentions(message.content, message.guild),
            author_id=message.author.id,
            author_name=message.author.name,
            author_display_name=message.author.display_name,
            channel_id=channel_id,
            channel_name=getattr(message.channel, "name", "DM"),
            guild_id=message.guild.id if message.guild else None,
            guild_name=message.guild.name if message.guild else None,
            message_id=message.id,
            is_mention=self.user in message.mentions if self.user else False,
            mentioned_users=[u.id for u in message.mentions],
            reply_to_id=reply_to_id,
            reply_to_author=reply_to_author,
            reply_to_content=reply_to_content,
            reply_to_is_bot=reply_to_is_bot,
            is_bot=message.author.bot,
            is_self=message.author == self.user,
            timestamp=self._format_timestamp(message.created_at),
            attachments=[MediaAttachment.from_discord(a) for a in message.attachments],
            stickers=[MediaSticker.from_discord(s) for s in message.stickers],
            custom_emojis=parse_custom_emojis(message.content),
        )

    async def on_message(self, message: discord.Message) -> None:
        """Normalize an allowed incoming message and deliver it to consumers."""
        if message.author == self.user:
            return
        if not self._is_watched(message.channel.id):
            return

        self.remember_message(message)
        discord_msg = await self.build_message(message)

        for listener in list(self._feed_listeners):
            listener.on_discord_message(discord_msg)

        if not self.enqueue_messages:
            return
        await self._message_queue.put(discord_msg)
        logger.info(
            "Message queued",
            extra={
                "author": message.author.display_name,
                "channel": discord_msg.channel_name,
                "queue_size": self._message_queue.qsize(),
                "is_mention": discord_msg.is_mention,
                "attachments": len(discord_msg.attachments),
                "stickers": len(discord_msg.stickers),
                "custom_emojis": len(discord_msg.custom_emojis),
            },
        )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Forward user-visible content edits to listeners."""
        message = payload.message
        if message.author == self.user or not self._is_watched(payload.channel_id):
            return
        # Embed resolution also arrives as an update; only real edits carry edited_at.
        if message.edited_at is None:
            return
        recent = self._lookup_recent(payload.channel_id, message.id)
        old = None
        if payload.cached_message is not None:
            old = payload.cached_message.content
        elif recent is not None:
            old = recent.content_preview
        if recent is not None:
            recent.content_preview = message.content[:100] if message.content else ""
        for listener in list(self._feed_listeners):
            listener.on_discord_edit(message, old)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Forward a single deletion to listeners."""
        if self._is_watched(payload.channel_id):
            for listener in list(self._feed_listeners):
                listener.on_discord_delete(payload.channel_id, [payload.message_id])

    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        """Forward a bulk deletion to listeners."""
        if self._is_watched(payload.channel_id):
            ids = sorted(payload.message_ids)
            for listener in list(self._feed_listeners):
                listener.on_discord_delete(payload.channel_id, ids)

    async def get_message(self) -> DiscordMessage:
        """Wait for and return the next normalized Discord message."""
        return await self._message_queue.get()

    def set_output_context(self, channel_id: int) -> None:
        """Set the channel that subsequent output should target."""
        self._current_channel_id = channel_id

    def is_readonly_channel(self, channel_id: int) -> bool:
        """Return whether channel policy forbids bot output."""
        return channel_id in self.readonly_channel_ids

    def find_message_id(self, reference: str, channel_id: int) -> int | None:
        """Resolve an ``m<digits>`` tag, ``#N`` position, short ID, or author name.

        ``#N`` counts from the oldest message of the last snapshot fetch,
        matching the numbering the snapshot prompt shows.
        """
        reference = reference.strip()
        recent = self._recent_messages.get(channel_id, deque())

        tag = MSG_TAG_PATTERN.match(reference)
        if tag:
            digits = tag.group(1)
            for msg in reversed(recent):
                if str(msg.message_id).endswith(digits):
                    return msg.message_id
            return None

        if reference.startswith("#") and reference[1:].isdigit():
            ids = self._history_ids.get(channel_id, [])
            n = int(reference[1:])
            if 1 <= n <= len(ids):
                return ids[n - 1]
            return None

        if ".." in reference:
            for msg in recent:
                if msg.short_id == reference:
                    return msg.message_id
            return None

        ref_lower = reference.lower()
        for msg in reversed(recent):
            if msg.author_name.lower() == ref_lower:
                return msg.message_id

        return None

    def find_user_id(self, name: str) -> int | None:
        """Resolve a cached account or display name to a user ID."""
        return self._user_cache.get(name.lower())

    def parse_mentions(self, content: str, guild: discord.Guild | None) -> str:
        """Replace Discord user markup with readable cached display names."""
        if not content:
            return content

        def replace_mention(match: re.Match) -> str:
            user_id = int(match.group(1))

            if self.user and user_id == self.user.id:
                return f"@{self.user.display_name}"

            if guild:
                member = guild.get_member(user_id)
                if member:
                    self._user_cache[member.display_name.lower()] = user_id
                    self._user_cache[member.name.lower()] = user_id
                    return f"@{member.display_name}"

            for name, uid in self._user_cache.items():
                if uid == user_id:
                    return f"@{name}"

            return f"@User({short_id(user_id)})"

        return DISCORD_MENTION_PATTERN.sub(replace_mention, content)

    async def resolve_text_channel(
        self, channel_id: int
    ) -> discord.TextChannel | discord.Thread | None:
        """Return a messageable channel from cache or the API, else None."""
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException as e:
                logger.warning(
                    "Failed to fetch channel",
                    extra={"channel_id": channel_id, "error": str(e)},
                )
                return None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning(
                "Channel not messageable",
                extra={
                    "channel_id": channel_id,
                    "channel_type": type(channel).__name__,
                },
            )
            return None
        return channel

    async def send_message(
        self,
        content: str,
        channel_id: int | None = None,
        reply_to_id: int | None = None,
        mentions: list[int] | None = None,
    ) -> discord.Message | None:
        """Send content to an allowed text channel with optional reply metadata."""
        target_channel_id = channel_id or self._current_channel_id

        if not target_channel_id:
            logger.warning("No target channel for send_message")
            return None

        if self.is_readonly_channel(target_channel_id):
            logger.debug(
                "Skipping send to read-only channel",
                extra={"channel_id": target_channel_id},
            )
            return None

        channel = await self.resolve_text_channel(target_channel_id)
        if channel is None:
            return None

        try:
            reference = None
            if reply_to_id:
                reference = discord.MessageReference(
                    message_id=reply_to_id,
                    channel_id=target_channel_id,
                )

            final_content = content
            if mentions:
                mention_strs = [f"<@{uid}>" for uid in mentions]
                final_content = " ".join(mention_strs) + " " + content

            logger.debug(
                "Sending Discord message",
                extra={
                    "channel_id": target_channel_id,
                    "reply_to": reply_to_id,
                    "content_preview": final_content[:50] if final_content else "",
                },
            )
            sent = await channel.send(final_content, reference=reference)
            self.remember_message(sent)
            return sent
        except discord.DiscordException as e:
            logger.error("Failed to send Discord message", extra={"error": str(e)})
            return None

    def get_bot_user_id(self) -> int | None:
        """Return the connected bot's user ID when available."""
        return self.user.id if self.user else None

    def has_pending_messages(self) -> bool:
        """Return whether normalized input is waiting in the queue."""
        return not self._message_queue.empty()

    def pending_message_count(self) -> int:
        """Return the number of normalized messages awaiting input."""
        return self._message_queue.qsize()
