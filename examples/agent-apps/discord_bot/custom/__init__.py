"""Expose the Discord bot's shared I/O, feed, triggers, and emoji tools.

This package provides:
- DiscordChannelPlugin: Binds one creature to one channel (incremental feed)
- DiscordFeedTrigger: Delivers that channel's activity as turns
- DiscordInputModule / DiscordOutputModule: Batch-snapshot I/O (ephemeral mode)
- DiscordPingTrigger / DiscordIdleTrigger: Mention and inactivity triggers
- EmojiSearchTool / EmojiListTool / EmojiGetTool: Guild emoji lookup

Config files load the entry modules in this folder through ModuleLoader,
which temporarily puts this directory on sys.path so ``botcore`` resolves.
For the package-import pathway (``from custom import ...``) this __init__
adds the same directory.
"""

import sys
from pathlib import Path

# Mirror ModuleLoader's path setup so ``botcore`` also resolves through the
# package-import pathway.
_CUSTOM_DIR = str(Path(__file__).parent)
if _CUSTOM_DIR not in sys.path:
    sys.path.insert(0, _CUSTOM_DIR)

from botcore.emoji_db import EmojiDatabase, EmojiRecord, get_emoji_db, save_emoji_db
from discord_feed import DiscordChannelPlugin, DiscordFeedTrigger
from discord_io import (
    DiscordClient,
    DiscordInputModule,
    DiscordMessage,
    DiscordOutputModule,
    create_discord_io,
)
from discord_trigger import (
    DiscordActivityMonitor,
    DiscordIdleTrigger,
    DiscordPingTrigger,
)
from emoji_search import (
    EmojiGetTool,
    EmojiListTool,
    EmojiSearchTool,
    create_emoji_tools,
)

__all__ = [
    "DiscordChannelPlugin",
    "DiscordFeedTrigger",
    "DiscordClient",
    "DiscordInputModule",
    "DiscordOutputModule",
    "DiscordMessage",
    "DiscordPingTrigger",
    "DiscordIdleTrigger",
    "DiscordActivityMonitor",
    "create_discord_io",
    "EmojiSearchTool",
    "EmojiListTool",
    "EmojiGetTool",
    "create_emoji_tools",
    "EmojiDatabase",
    "EmojiRecord",
    "get_emoji_db",
    "save_emoji_db",
]
