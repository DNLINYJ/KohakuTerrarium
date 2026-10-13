"""Track native handles and file occupancy for a session memory index."""

import threading
from typing import Any

from kohakuterrarium.session.resource_gate import acquire_legacy
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


class MemoryResources:
    """Retain occupancy until all tracked index handles are disposed."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._lease = acquire_legacy(path, "memory_open")
        self._handles: list[tuple[Any, str]] = []
        self._released = False
        self._certain = True

    def open(self, factory, *args, native_attr: str, **kwargs):
        """Open and retain one native index table."""
        with self._lock:
            if self._released:
                raise RuntimeError("SessionMemory is closed")
            try:
                handle = factory(*args, **kwargs)
                self._handles.append((handle, native_attr))
            except BaseException:
                self._certain = False
                raise
            return handle

    def drop(self, handle: Any) -> None:
        """Dispose a temporary index handle before the enclosing memory closes."""
        with self._lock:
            for index, (table, attr) in enumerate(self._handles):
                if table is handle:
                    try:
                        delattr(table, attr)
                    except BaseException:
                        self._certain = False
                        raise
                    else:
                        self._handles.pop(index)
                    return

    def close(self) -> None:
        """Close index state and drop native references, retaining uncertain occupancy."""
        with self._lock:
            if self._released:
                return
            try:
                for table, attr in self._handles:
                    if attr == "_inner":
                        daemon = getattr(table, "_daemon_thread", None)
                        try:
                            table.close()
                        except Exception:
                            self._certain = False
                            logger.warning("Memory state close failed", exc_info=True)
                        if daemon is not None and daemon.is_alive():
                            self._certain = False
                    try:
                        delattr(table, attr)
                    except AttributeError:
                        continue
                    except Exception:
                        self._certain = False
                        logger.warning(
                            "Memory native handle drop failed", exc_info=True
                        )
            except BaseException:
                self._certain = False
                raise
            finally:
                self._released = True
                self._lease.finish(source_released=self._certain)
                self._handles.clear()
