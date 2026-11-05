"""Operator choices about which packages may contribute dashboard UI.

Stores only explicit choices (``package -> bool``). A package with no stored
choice falls back to a default the caller decides, so this store stays free
of any knowledge about users or hosts. Reads are cached for
``CHOICES_WINDOW_SECONDS``; writes refresh the cache at once.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from kohakuterrarium.utils.config_dir import config_dir
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

CHOICES_WINDOW_SECONDS = 2.0

_lock = threading.Lock()
_cache: dict[str, object] = {"path": None, "at": None, "choices": {}}


def ui_extensions_path() -> Path:
    """The JSON file holding per-package UI enable choices."""
    return config_dir() / "ui_extensions.json"


def invalidate_choices() -> None:
    """Drop the cached choices so the next read goes to disk."""
    with _lock:
        _cache["at"] = None


def _read(path: Path) -> dict[str, bool]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Unreadable UI extension choices", path=str(path), error=str(e))
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, bool)}


def load_choices() -> dict[str, bool]:
    """Return stored choices; missing or malformed files give an empty map."""
    path = ui_extensions_path()
    now = time.monotonic()
    with _lock:
        at = _cache["at"]
        if (
            at is not None
            and _cache["path"] == path
            and now - at < CHOICES_WINDOW_SECONDS
        ):
            return dict(_cache["choices"])
    choices = _read(path)
    with _lock:
        _cache.update(path=path, at=now, choices=choices)
    return dict(choices)


def is_enabled(package: str, *, default: bool) -> bool:
    """Whether ``package`` may contribute UI, using ``default`` when unset."""
    return load_choices().get(package, default)


def set_enabled(package: str, enabled: bool | None) -> dict[str, bool]:
    """Store a choice for ``package``; ``None`` clears it back to the default."""
    path = ui_extensions_path()
    with _lock:
        choices = _read(path)
        if enabled is None:
            choices.pop(package, None)
        else:
            choices[package] = bool(enabled)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=".ui_extensions.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(choices, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _cache.update(path=path, at=time.monotonic(), choices=dict(choices))
    return dict(choices)
