"""Filter, deduplicate, and send controller output through Discord.

Uses the shared DiscordClient to send messages. Supports reply markers,
mentions, keyword filtering, and deduplication. ``send`` reports exactly
what happened so callers never mistake a filtered message for a sent one.
"""

import asyncio
import random
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord

from kohakuterrarium.modules.output import BaseOutputModule
from kohakuterrarium.utils.logging import get_logger

from .client import DiscordClient, get_client
from .input import DiscordInputModule
from .paths import AGENT_ROOT
from .records import msg_tag

logger = get_logger("kohakuterrarium.custom.discord_output")

# Whole-message role words and leading protocol fragments are generation artifacts.
ARTIFACT_EXACT = {"user", "assistant", "---", "```"}
ARTIFACT_PREFIXES = ("[tool", "background job", "<discord_update", "<discord_backfill")


@dataclass
class SendResult:
    """Outcome of one send attempt, phrased for a model to read."""

    sent: bool
    reason: str = ""
    message_id: int | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """Return a one-line report: what was sent, or why it was not."""
        if self.sent and self.message_id is not None:
            head = f"Sent as {msg_tag(self.message_id)}"
        elif self.sent:
            head = "Sent"
        else:
            head = f"NOT SENT: {self.reason}"
        if self.notes:
            head += " (" + "; ".join(self.notes) + ")"
        return head


class DiscordOutputModule(BaseOutputModule):
    """Send guarded output through the Discord client shared by input.

    Supports special markers in output:
    - [reply:Username], [reply:m123456] or [reply:#N] - reply to a message
    - [@Username] - mention a user

    Supports keyword filtering and consecutive message deduplication.
    """

    REPLY_PATTERN = re.compile(r"\[reply:([^\]]+)\]", re.IGNORECASE)
    MENTION_PATTERN = re.compile(r"\[@([^\]]+)\]")

    def __init__(
        self,
        client: DiscordClient | None = None,
        client_name: str = "default",
        filtered_keywords: list[str] | None = None,
        keywords_file: str | None = None,
        drop_base_chance: float = 0.25,
        drop_increment: float = 0.15,
        drop_max_chance: float = 0.7,
        dedup_threshold: float = 0.85,
        dedup_window: int = 5,
    ):
        """Initialize filtering, load shedding, and deduplication policy.

        Args:
            client: Shared Discord client
            client_name: Name to look up in shared client registry
            filtered_keywords: Keywords to replace with [filtered]
            keywords_file: Path to keywords file (one per line)
            drop_base_chance: Base chance to drop when batch input is pending
            drop_increment: Additional chance per extra pending message
            drop_max_chance: Maximum drop chance cap
            dedup_threshold: Similarity threshold for deduplication (0.0-1.0)
            dedup_window: Number of recent messages to check for duplicates
        """
        super().__init__()
        self.client = client
        self.client_name = client_name
        self._buffer: list[str] = []

        self.drop_base_chance = drop_base_chance
        self.drop_increment = drop_increment
        self.drop_max_chance = drop_max_chance

        self.dedup_threshold = dedup_threshold
        self._recent_outputs: deque[str] = deque(maxlen=dedup_window)

        self._filtered_keywords: set[str] = set()
        if filtered_keywords:
            self._filtered_keywords.update(kw.lower() for kw in filtered_keywords)

        if keywords_file:
            self._load_keywords_file(keywords_file)

        if self._filtered_keywords:
            logger.info(
                "Keyword filter enabled",
                extra={"keyword_count": len(self._filtered_keywords)},
            )

        self._typing_task: asyncio.Task | None = None

        logger.info(
            "Initializing Discord output module",
            extra={
                "client_name": client_name,
                "drop_base": drop_base_chance,
                "dedup_threshold": dedup_threshold,
            },
        )

    def _load_keywords_file(self, filepath: str) -> None:
        """Load one case-insensitive filtered keyword per non-comment line.

        Relative paths resolve against the bot folder, not the process cwd.
        """
        path = Path(filepath)
        if not path.is_absolute():
            path = AGENT_ROOT / path
        if not path.exists():
            logger.warning("Keywords file not found", extra={"path": str(path)})
            return

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._filtered_keywords.add(line.lower())
        except OSError as e:
            logger.warning("Failed to load keywords file", extra={"error": str(e)})

    def _filter_keywords(self, content: str) -> tuple[str, int]:
        """Redact configured keywords without regard to case; return the hit count."""
        hits = 0
        result = content
        for keyword in self._filtered_keywords:
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
            result, n = pattern.subn("[filtered]", result)
            hits += n
        return result, hits

    def _similarity(self, a: str, b: str) -> float:
        """Estimate normalized character overlap between two messages."""
        norm_a = " ".join(a.lower().split())
        norm_b = " ".join(b.lower().split())

        if norm_a == norm_b:
            return 1.0

        if not norm_a or not norm_b:
            return 0.0

        len_a, len_b = len(norm_a), len(norm_b)

        if norm_a in norm_b or norm_b in norm_a:
            return min(len_a, len_b) / max(len_a, len_b)

        common = 0
        b_chars = list(norm_b)
        for c in norm_a:
            if c in b_chars:
                common += 1
                b_chars.remove(c)

        return (2 * common) / (len_a + len_b)

    def _is_duplicate(self, content: str) -> bool:
        """Return whether content is too similar to a recent sent message."""
        for recent in self._recent_outputs:
            if self._similarity(content, recent) >= self.dedup_threshold:
                return True
        return False

    def _ensure_client(self) -> DiscordClient | None:
        """Return the explicit client or resolve it from the shared registry."""
        if self.client is None:
            self.client = get_client(self.client_name)
            if self.client:
                logger.debug("Retrieved Discord client from registry")
            else:
                logger.warning("Discord client not found in registry")
        return self.client

    async def _typing_loop(self, channel: discord.TextChannel | discord.Thread) -> None:
        """Refresh Discord's expiring typing indicator until cancelled."""
        try:
            while True:
                await channel.typing()
                # Refresh before Discord's roughly ten-second indicator expires.
                await asyncio.sleep(8)
        except asyncio.CancelledError:
            pass
        except discord.DiscordException:
            pass

    async def on_processing_start(self) -> None:
        """Start one typing indicator for the active writable channel."""
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

        client = self._ensure_client()
        if not client or not client._current_channel_id:
            return

        channel_id = client._current_channel_id

        if client.is_readonly_channel(channel_id):
            return

        channel = await client.resolve_text_channel(channel_id)
        if channel is not None:
            self._typing_task = asyncio.create_task(self._typing_loop(channel))
            logger.debug("Started typing indicator")

    async def on_processing_end(self) -> None:
        """Cancel and await the active typing indicator."""
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
            try:
                await self._typing_task
            except asyncio.CancelledError:
                pass
            self._typing_task = None
            logger.debug("Stopped typing indicator")

    def _parse_markers(
        self, content: str, channel_id: int, notes: list[str]
    ) -> tuple[str, int | None, list[int]]:
        """Resolve reply and mention markers, then remove them from content."""
        client = self._ensure_client()
        if not client:
            return content, None, []

        reply_to_id = None
        mention_ids = []

        reply_match = self.REPLY_PATTERN.search(content)
        if reply_match:
            ref = reply_match.group(1).strip()
            reply_to_id = client.find_message_id(ref, channel_id)
            if reply_to_id is None:
                notes.append(f"reply target {ref!r} not found, sent without reply")
            content = self.REPLY_PATTERN.sub("", content)

        for match in self.MENTION_PATTERN.finditer(content):
            name = match.group(1).strip()
            user_id = client.find_user_id(name)
            if user_id:
                mention_ids.append(user_id)
            else:
                notes.append(f"unknown user @{name}, mention dropped")

        content = self.MENTION_PATTERN.sub("", content)

        return content.strip(), reply_to_id, mention_ids

    def _artifact_reason(self, content: str) -> str | None:
        """Return why ``content`` looks like a generation artifact, else None."""
        lower = content.lower().strip()
        if lower in ARTIFACT_EXACT or lower.startswith(ARTIFACT_PREFIXES):
            return "looks like protocol/markup, not a chat message"
        stripped = content.replace(" ", "").replace("\n", "")
        if len(stripped) <= 3 and not any(c.isalnum() for c in stripped):
            return "punctuation-only fragment"
        if len(set(stripped)) <= 2 and len(stripped) > 1:
            return "repetitive low-entropy text"
        return None

    async def send(
        self,
        content: str,
        channel_id: int | None = None,
        *,
        allow_drop: bool = True,
    ) -> SendResult:
        """Apply output policy and send one message, reporting the outcome.

        ``allow_drop`` enables the random load-shedding used by batch input,
        where pending messages mean the reply is probably stale.
        """
        client = self._ensure_client()
        if not client:
            return SendResult(False, "Discord client is not connected")

        if allow_drop:
            pending_count = client.pending_message_count()
            if pending_count > 0:
                drop_chance = min(
                    self.drop_max_chance,
                    self.drop_base_chance + (pending_count - 1) * self.drop_increment,
                )
                if random.random() < drop_chance:
                    logger.info(
                        "Dropping response - new messages pending",
                        extra={"pending_count": pending_count},
                    )
                    return SendResult(False, "newer messages are pending (load shed)")

        clean_content = content.strip()
        if not clean_content:
            return SendResult(False, "empty message")
        if self._is_duplicate(clean_content):
            return SendResult(False, "near-duplicate of a message you just sent")
        artifact = self._artifact_reason(clean_content)
        if artifact:
            return SendResult(False, artifact)

        target = channel_id or client._current_channel_id
        if not target:
            return SendResult(False, "no target channel")
        if client.is_readonly_channel(target):
            return SendResult(False, "channel is read-only")

        notes: list[str] = []
        final_content, reply_to_id, mention_ids = self._parse_markers(
            clean_content, target, notes
        )
        if not final_content:
            return SendResult(False, "nothing left after removing markers", notes=notes)

        final_content, hits = self._filter_keywords(final_content)
        if hits:
            notes.append(f"{hits} filtered keyword(s) replaced")

        sent = await client.send_message(
            final_content,
            channel_id=target,
            reply_to_id=reply_to_id,
            mentions=mention_ids,
        )
        if sent is None:
            return SendResult(False, "Discord rejected the send", notes=notes)
        self._recent_outputs.append(final_content)
        return SendResult(True, message_id=sent.id, notes=notes)

    async def write(self, content: str) -> None:
        """Send one complete message through the output policy."""
        result = await self.send(content)
        if not result.sent:
            logger.info("Output not sent", extra={"reason": result.reason})

    async def write_stream(self, chunk: str) -> None:
        """Buffer a streaming chunk until the turn is flushed."""
        self._buffer.append(chunk)

    async def flush(self) -> None:
        """Join and send all buffered streaming chunks as one message."""
        if self._buffer:
            content = "".join(self._buffer)
            self._buffer.clear()
            await self.write(content)


def create_discord_io(
    token: str | None = None,
    token_env: str = "DISCORD_BOT_TOKEN",
    channel_ids: list[int] | None = None,
    readonly_channel_ids: list[int] | None = None,
    history_limit: int = 20,
    filtered_keywords: list[str] | None = None,
    keywords_file: str | None = None,
):
    """Create paired modules that share one Discord client."""
    input_module = DiscordInputModule(
        token=token,
        token_env=token_env,
        channel_ids=channel_ids,
        readonly_channel_ids=readonly_channel_ids,
        history_limit=history_limit,
    )

    output_module = DiscordOutputModule(
        client=input_module.get_client(),
        filtered_keywords=filtered_keywords,
        keywords_file=keywords_file,
    )

    return input_module, output_module
