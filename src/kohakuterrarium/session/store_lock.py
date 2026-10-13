"""Enforce single-writer session access across processes.

Read-only consumers remain lock-free, and operating-system lock ownership is
released automatically when a process exits.
"""

from collections.abc import Callable, Iterable
from typing import Any

from kohakuterrarium.errors import SessionLockedError
from kohakuterrarium.utils.file_lock import FileLock, FileLockBusy
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

# SessionStore KVault table attributes in close order; the FTS TextVault is
# handled apart.
TABLE_ATTRS: tuple[str, ...] = (
    "events",
    "meta",
    "state",
    "channels",
    "subagents",
    "jobs",
    "conversation",
    "turn_rollup",
)


def acquire_writer_lock(path: str) -> FileLock:
    """Acquire the writer lock for session ``path``.

    Return the held lock for release at store close. If another writer owns it,
    raise :class:`SessionLockedError` with the holder process identifier.
    """
    lock = FileLock(path + ".lock")
    try:
        lock.acquire()
    except FileLockBusy as exc:
        raise SessionLockedError(
            f"Session is already open for writing by another process "
            f"(pid {exc.holder_pid}): {path}. Close the other instance, or "
            f"open it read-only to view.",
            holder_pid=exc.holder_pid,
        ) from exc
    return lock


def release_writer_lock(lock: "FileLock | None") -> None:
    """Release ``lock`` if present. Tolerates ``None`` (read-only opens)."""
    if lock is not None:
        lock.release()


def close_tables(
    tables,
    fts,
    lock: "FileLock | None",
    companion_closers: Iterable[Callable[[], None]] = (),
) -> bool:
    """Close every KVault table, drop native handles, release the lock.

    Companion resources close first in LIFO order. Native vault handles are
    explicitly dropped because their public close paths can retain SQLite file
    handles on Windows. The writer lock is released even if cleanup fails.
    Return True only when companions succeeded and native handles were dropped.
    """
    released = True
    try:
        for closer in reversed(list(companion_closers)):
            try:
                closer()
            except Exception:
                released = False
                logger.warning("Companion closer failed at close", exc_info=True)
        for table in tables:
            daemon = getattr(table, "_daemon_thread", None)
            table.close()
            if daemon is not None and daemon.is_alive():
                released = False
        for table in tables:
            try:
                del table._inner
            except AttributeError:
                pass
            except Exception:
                released = False
                logger.warning("Native table handle drop failed", exc_info=True)
        if fts is not None:
            try:
                del fts._vault
            except AttributeError:
                pass
            except Exception:
                released = False
                logger.warning("Native FTS handle drop failed", exc_info=True)
    except BaseException:
        released = False
        raise
    finally:
        release_writer_lock(lock)
    return released


def _drop_native_handle(obj: Any, attr: str, *, table: str, path: str) -> bool:
    try:
        delattr(obj, attr)
    except AttributeError:
        return False
    except Exception:
        logger.warning(
            "partial-open native handle drop failed",
            table=table,
            path=path,
            exc_info=True,
        )
        return False
    return True


def discard_partial_open(tables: Iterable[tuple[str, Any]], fts, path: str) -> bool:
    """Release tables opened before a store construction failure.

    ``tables`` pairs each attribute name with its KVault, or ``None`` when the
    failure happened before that table opened. Every step is best-effort so one
    failing table cannot keep the others, or the events cache daemon, alive.
    Return True only after every table and FTS handle was assigned and dropped.
    """
    released = True
    for name, table in tables:
        if table is None:
            released = False
            continue
        daemon = getattr(table, "_daemon_thread", None)
        for step in (table.disable_cache, table.close):
            try:
                step()
            except Exception:
                released = False
                logger.warning(
                    "partial-open cleanup step failed",
                    table=name,
                    path=path,
                    exc_info=True,
                )
        if daemon is not None and daemon.is_alive():
            released = False
        if not _drop_native_handle(table, "_inner", table=name, path=path):
            released = False
    if fts is None:
        return False
    try:
        fts.close()
    except Exception:
        released = False
        logger.warning(
            "partial-open cleanup step failed",
            table="fts",
            path=path,
            exc_info=True,
        )
    if not _drop_native_handle(fts, "_vault", table="fts", path=path):
        released = False
    return released
