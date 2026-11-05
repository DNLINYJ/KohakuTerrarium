"""Declare how a tool's media results are transported and presented.

:class:`MediaPolicy` controls artifact persistence, inline fallback, and
collapsed previews. A tool sets its default on the class; a single result
may override it through ``ToolResult.metadata["media_policy"]``.
"""

from dataclasses import dataclass
from typing import Any

METADATA_KEY = "media_policy"


@dataclass(frozen=True)
class MediaPolicy:
    """Transport and presentation rules for inline media in a tool result.

    ``persist`` copies inline (``data:``) media into the session artifact
    store and lists the copy as a saved artifact. ``persist=False`` leaves
    inline bytes untouched. Live ``file://`` references always pass through.

    ``inline_fallback`` retains inline bytes if artifact storage is unavailable
    or fails, instead of replacing the image with a text placeholder.

    ``pinned`` keeps the media visible when the tool block is collapsed. Set
    it False for media that is context rather than output, such as a file the
    creature looked at.
    """

    persist: bool = True
    pinned: bool = True
    inline_fallback: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "persist": self.persist,
            "pinned": self.pinned,
            "inline_fallback": self.inline_fallback,
        }

    @classmethod
    def coerce(cls, value: Any, base: "MediaPolicy | None" = None) -> "MediaPolicy":
        """Build a policy from a policy, a mapping, or nothing, over ``base``."""
        base = base or cls()
        if isinstance(value, MediaPolicy):
            return value
        if isinstance(value, dict):
            return cls(
                persist=bool(value.get("persist", base.persist)),
                pinned=bool(value.get("pinned", base.pinned)),
                inline_fallback=bool(
                    value.get("inline_fallback", base.inline_fallback)
                ),
            )
        return base


def resolve_media_policy(tool: Any, metadata: dict[str, Any] | None) -> MediaPolicy:
    """Return the effective policy: the result override on top of the tool default."""
    base = MediaPolicy.coerce(getattr(tool, "media_policy", None))
    override = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    return MediaPolicy.coerce(override, base)


__all__ = ["METADATA_KEY", "MediaPolicy", "resolve_media_policy"]
