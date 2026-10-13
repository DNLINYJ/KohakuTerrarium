"""Locate the bot folder that owns config, prompts, and memory."""

from pathlib import Path

# botcore/ lives at <agent root>/custom/botcore.
AGENT_ROOT = Path(__file__).resolve().parents[2]
