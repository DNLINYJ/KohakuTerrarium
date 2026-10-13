"""Render channel-feed activity into the text and images a creature reads.

Every feed delivery is wrapped in a ``<discord_update>`` or
``<discord_backfill>`` block so the model can tell Discord chat apart
from anything typed by its operator.
"""

from dataclasses import dataclass, field
from typing import Literal

from kohakuterrarium.llm.message import ContentPart, ImagePart, TextPart

from .hub import ChannelFeed, FeedItem
from .records import DiscordMessage, msg_tag, short_id
from .images import process_multiple_images

REPLY_PREVIEW_CHARS = 60
EDIT_PREVIEW_CHARS = 200


@dataclass
class ImageOptions:
    """Limits for turning message media into vision input."""

    enabled: bool = True
    include_attachments: bool = True
    include_stickers: bool = True
    include_emojis: bool = True
    detail: Literal["auto", "low", "high"] = "auto"
    max_per_message: int = 4
    max_total: int = 8
    gif_sample_frames: list[str] = field(
        default_factory=lambda: ["first", "middle", "last"]
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _author(msg: DiscordMessage) -> str:
    if msg.author_display_name != msg.author_name:
        return f"{msg.author_display_name}|{msg.author_name}({msg.short_author_id})"
    return f"{msg.author_name}({msg.short_author_id})"


def _media_markers(msg: DiscordMessage) -> str:
    markers = []
    for att in msg.attachments:
        anim = " (animated)" if att.is_animated else ""
        kind = "attachment" if att.is_image else "file"
        markers.append(f"[{kind}:{att.filename}{anim}]")
    for sticker in msg.stickers:
        anim = " (animated)" if sticker.is_animated else ""
        markers.append(f"[sticker:{sticker.name}{anim}]")
    if msg.custom_emojis:
        names = ", ".join(f":{e.name}:" for e in msg.custom_emojis)
        markers.append(f"[emojis: {names}]")
    return " ".join(markers)


def render_message(msg: DiscordMessage) -> str:
    """Render one message as a single tagged line."""
    flags = ""
    if msg.is_self:
        flags += "[SELF] "
    elif msg.is_bot:
        flags += "[BOT] "
    if msg.is_mention:
        flags += "[PINGED] "

    reply = ""
    if msg.reply_to_id:
        target = msg_tag(msg.reply_to_id)
        if msg.reply_to_author:
            bot = "[BOT]" if msg.reply_to_is_bot else ""
            preview = msg.reply_to_content or ""
            quote = f' "{_clip(preview, REPLY_PREVIEW_CHARS)}"' if preview else ""
            reply = f" ↩{target} {bot}{msg.reply_to_author}{quote}"
        else:
            reply = f" ↩{target}"

    media = _media_markers(msg)
    media = f" {media}" if media else ""
    tag = msg_tag(msg.message_id)
    return f"[{msg.timestamp}] {tag} {flags}{_author(msg)}{reply}: {msg.content}{media}"


def render_item(item: FeedItem) -> str:
    """Render a buffered message, edit, or deletion as one line."""
    match item.kind:
        case "message" if item.message is not None:
            return render_message(item.message)
        case "edit":
            was = ""
            if item.old_content is not None:
                was = f" (was: {_clip(item.old_content, EDIT_PREVIEW_CHARS)})"
            return f"[edited] {msg_tag(item.message_id)} {item.author}: {item.content}{was}"
        case "delete":
            return f"[deleted] {msg_tag(item.message_id)} {item.author}"
    raise ValueError(f"Unrenderable feed item: {item.kind}")


def render_block(tag: str, attrs: dict[str, str], lines: list[str]) -> str:
    """Wrap rendered lines in a ``<tag attr="...">`` block."""
    head = " ".join(f'{k}="{v}"' for k, v in attrs.items() if v)
    body = "\n".join(lines) if lines else "(no messages)"
    return f"<{tag} {head}>\n{body}\n</{tag}>"


def channel_attrs(feed: ChannelFeed) -> dict[str, str]:
    """Build the channel/server/identity attributes for a block header."""
    name = f"#{feed.channel_name}" if feed.channel_name else short_id(feed.channel_id)
    return {
        "channel": name,
        "server": feed.guild_name,
        "you": feed.client.get_bot_identity(),
    }


def _image_items(
    messages: list[DiscordMessage], opts: ImageOptions
) -> list[tuple[str, str, str, bool]]:
    items: list[tuple[str, str, str, bool]] = []
    for msg in messages:
        tag = msg_tag(msg.message_id)
        candidates: list[tuple[str, str, str, bool]] = []
        if opts.include_attachments:
            candidates += [
                (a.url, "attachment", f"{tag} {a.filename}", a.is_animated)
                for a in msg.attachments
                if a.is_image
            ]
        if opts.include_stickers:
            # The image pipeline cannot rasterize Lottie vectors.
            candidates += [
                (s.url, "sticker", f"{tag} {s.name}", s.is_animated)
                for s in msg.stickers
                if s.format_type != "lottie"
            ]
        if opts.include_emojis:
            candidates += [
                (e.url, "emoji", f"{tag} :{e.name}:", e.animated)
                for e in msg.custom_emojis
            ]
        items.extend(candidates[: opts.max_per_message])
    # Newest media matters most when the total budget is exceeded.
    return items[-opts.max_total :] if opts.max_total > 0 else []


async def build_images(
    messages: list[DiscordMessage], opts: ImageOptions
) -> list[ImagePart]:
    """Download and encode media from ``messages`` as vision parts."""
    if not opts.enabled:
        return []
    items = _image_items(messages, opts)
    if not items:
        return []
    processed = await process_multiple_images(
        items, max_images=opts.max_total, gif_sample_positions=opts.gif_sample_frames
    )
    parts = []
    for img in processed:
        name = img.source_name
        if img.frame_info:
            name = f"{name} ({img.frame_info})"
        parts.append(
            ImagePart(
                url=img.data_url,
                detail=opts.detail,
                source_type=img.source_type,
                source_name=name,
            )
        )
    return parts


def combine(text: str, images: list[ImagePart]) -> str | list[ContentPart]:
    """Return plain text, or text with an image index followed by the images."""
    if not images:
        return text
    index = "\n".join(
        f"Image {i}: {img.get_description()}" for i, img in enumerate(images, 1)
    )
    return [TextPart(text=f"{text}\n\nAttached images, in order:\n{index}"), *images]
