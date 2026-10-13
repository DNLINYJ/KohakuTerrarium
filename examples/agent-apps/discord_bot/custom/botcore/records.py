"""Normalized Discord message and media records shared by the bot modules."""

import re
from dataclasses import dataclass, field
from typing import Any

import discord


@dataclass
class MediaAttachment:
    """Store normalized metadata for a Discord file attachment."""

    url: str
    filename: str
    content_type: str | None
    size: int
    is_image: bool
    is_animated: bool = False  # Discord serves animated image attachments as GIFs.

    @classmethod
    def from_discord(cls, attachment: discord.Attachment) -> "MediaAttachment":
        """Normalize a discord.py attachment."""
        content_type = attachment.content_type or ""
        is_image = content_type.startswith("image/")
        is_animated = content_type == "image/gif"
        return cls(
            url=attachment.url,
            filename=attachment.filename,
            content_type=content_type,
            size=attachment.size,
            is_image=is_image,
            is_animated=is_animated,
        )


@dataclass
class MediaSticker:
    """Store normalized metadata for a Discord sticker."""

    name: str
    sticker_id: int
    url: str
    format_type: str  # "png", "apng", "lottie", "gif"
    is_animated: bool

    @classmethod
    def from_discord(cls, sticker: discord.StickerItem) -> "MediaSticker":
        """Normalize a discord.py sticker and its animation format."""
        format_type = "png"
        is_animated = False
        if hasattr(sticker, "format"):
            fmt = sticker.format
            if fmt == discord.StickerFormatType.png:
                format_type = "png"
            elif fmt == discord.StickerFormatType.apng:
                format_type = "apng"
                is_animated = True
            elif fmt == discord.StickerFormatType.lottie:
                format_type = "lottie"
                is_animated = True
            elif fmt == discord.StickerFormatType.gif:
                format_type = "gif"
                is_animated = True

        return cls(
            name=sticker.name,
            sticker_id=sticker.id,
            url=str(sticker.url),
            format_type=format_type,
            is_animated=is_animated,
        )


@dataclass
class CustomEmoji:
    """Store a custom emoji parsed from Discord message markup."""

    name: str
    emoji_id: int
    animated: bool
    url: str

    @classmethod
    def from_match(cls, name: str, emoji_id: int, animated: bool) -> "CustomEmoji":
        """Build emoji metadata from parsed markup fields."""
        ext = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
        return cls(
            name=name,
            emoji_id=emoji_id,
            animated=animated,
            url=url,
        )


# Discord encodes static and animated custom emoji as <:name:id> and <a:name:id>.
CUSTOM_EMOJI_PATTERN = re.compile(r"<(a?):(\w+):(\d+)>")


def short_id(full_id: int) -> str:
    """Abbreviate long Discord IDs while preserving recognizable edges."""
    s = str(full_id)
    if len(s) <= 8:
        return s
    return f"{s[:4]}..{s[-4:]}"


# Six trailing snowflake digits keep tags short while rarely colliding in one channel.
MSG_TAG_DIGITS = 6
MSG_TAG_PATTERN = re.compile(r"^m(\d{1,%d})$" % MSG_TAG_DIGITS)


def msg_tag(message_id: int) -> str:
    """Return the compact ``m<digits>`` tag the model uses to reference a message."""
    return f"m{str(message_id)[-MSG_TAG_DIGITS:]}"


def parse_custom_emojis(content: str) -> list[CustomEmoji]:
    """Extract custom emoji metadata from Discord markup."""
    emojis = []
    for match in CUSTOM_EMOJI_PATTERN.finditer(content):
        animated = bool(match.group(1))  # The optional "a" prefix marks animation.
        name = match.group(2)
        emoji_id = int(match.group(3))
        emojis.append(CustomEmoji.from_match(name, emoji_id, animated))
    return emojis


@dataclass
class DiscordMessage:
    """Represent one Discord message with reply and multimodal metadata."""

    content: str
    author_id: int
    author_name: str
    author_display_name: str
    channel_id: int
    channel_name: str
    guild_id: int | None
    guild_name: str | None
    message_id: int
    is_mention: bool
    mentioned_users: list[int]
    reply_to_id: int | None
    reply_to_author: str | None = None
    reply_to_content: str | None = None
    reply_to_is_bot: bool = False
    is_bot: bool = False
    is_self: bool = False
    timestamp: str = ""
    short_msg_id: str = ""
    short_author_id: str = ""

    attachments: list[MediaAttachment] = field(default_factory=list)
    stickers: list[MediaSticker] = field(default_factory=list)
    custom_emojis: list[CustomEmoji] = field(default_factory=list)

    def __post_init__(self):
        self.short_msg_id = short_id(self.message_id)
        self.short_author_id = short_id(self.author_id)

    def has_media(self) -> bool:
        """Return whether the message contains any supported media."""
        return bool(self.attachments or self.stickers or self.custom_emojis)

    def get_image_attachments(self) -> list[MediaAttachment]:
        """Return attachments classified as images."""
        return [a for a in self.attachments if a.is_image]

    def has_animated_content(self) -> bool:
        """Return whether any attachment, sticker, or emoji is animated."""
        if any(a.is_animated for a in self.attachments):
            return True
        if any(s.is_animated for s in self.stickers):
            return True
        if any(e.animated for e in self.custom_emojis):
            return True
        return False

    def to_context(self) -> dict[str, Any]:
        """Build the serializable metadata attached to a trigger event."""
        return {
            "source": "discord",
            "author_id": self.author_id,
            "author_name": self.author_name,
            "author_display_name": self.author_display_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "guild_id": self.guild_id,
            "guild_name": self.guild_name,
            "message_id": self.message_id,
            "short_msg_id": self.short_msg_id,
            "is_mention": self.is_mention,
            "mentioned_users": self.mentioned_users,
            "reply_to_id": self.reply_to_id,
            "has_attachments": len(self.attachments) > 0,
            "has_stickers": len(self.stickers) > 0,
            "has_custom_emojis": len(self.custom_emojis) > 0,
        }


@dataclass
class RecentMessage:
    """Retain the minimal message metadata needed for later references."""

    message_id: int
    short_id: str
    author_name: str
    author_id: int
    content_preview: str
    is_bot: bool = False
