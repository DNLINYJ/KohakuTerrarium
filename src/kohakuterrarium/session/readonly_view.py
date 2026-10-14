"""Selective, read-only session queries without writable source vault handles.

A file no live store holds open is read through a SQLite read transaction that
includes committed WAL rows, and KohakuVault only decodes selected values in an
isolated in-memory vault. A file that a live store in this process holds open is
read through that store, because a second SQLite library copy on the same file
crashes the process. Occupancy is enrolled before either path opens the file.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kohakuvault import KVault

from kohakuterrarium.session.errors import SessionNotReadyError
from kohakuterrarium.session.open_registry import live_store_for
from kohakuterrarium.session.resource_gate import ResourceLease, acquire_legacy
from kohakuterrarium.session.store import iter_kv_keys
from kohakuterrarium.utils.fs_path import coerce_fs_path
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

_TABLES = frozenset({"meta", "events", "state", "conversation"})
_CLOSED_DB = "Cannot operate on a closed database."


def _text(key: Any) -> str:
    return key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key


class SessionReadView:
    """One selectively decoded view of an existing session."""

    def __init__(self, path: str | Path) -> None:
        source = coerce_fs_path(path).expanduser().resolve(strict=True)
        self._live = None
        self._db = None
        self._decoder = None
        self._cursors: list[sqlite3.Cursor] = []
        self._tables: set[str] = set()
        self._closed = False
        self._source_open_attempted = False
        self._source_uncertain = False
        self._resource_lease: ResourceLease | None = acquire_legacy(source, "read_view")
        try:
            self._live = live_store_for(source)
            if self._live is not None:
                if not self._live.is_ready:
                    self._live = None
                    raise SessionNotReadyError(f"Session is still opening: {source}")
                return
            self._open_snapshot(source)
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                raise primary from cleanup
            raise

    def _open_snapshot(self, source: Path) -> None:
        self._source_open_attempted = True
        self._db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        self._drop_cursor(self._execute("BEGIN"))
        with self._query("SELECT name FROM sqlite_master WHERE type='table'") as rows:
            self._tables = {row[0] for row in rows}
        self._decoder = KVault(":memory:", enable_wal=False, cache_kb=1024)
        self._decoder.enable_auto_pack()

    def _raise_closed(self) -> None:
        raise sqlite3.ProgrammingError(_CLOSED_DB)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        if self._closed or self._db is None:
            self._raise_closed()
        cursor = self._db.execute(sql, params)
        try:
            if self._closed or self._db is None:
                self._raise_closed()
            self._cursors.append(cursor)
        except BaseException as primary:
            self._source_uncertain = True
            try:
                cursor.close()
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        return cursor

    @contextmanager
    def _query(self, sql: str, params: tuple[Any, ...] = ()):
        cursor = self._execute(sql, params)
        try:
            yield cursor
        except BaseException as primary:
            try:
                self._drop_cursor(cursor)
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        else:
            self._drop_cursor(cursor)

    def _drop_cursor(self, cursor: sqlite3.Cursor | None) -> bool:
        if cursor is None or cursor not in self._cursors:
            return True
        try:
            cursor.close()
        except sqlite3.ProgrammingError:
            self._source_uncertain = True
            self._cursors.remove(cursor)
            return False
        except BaseException:
            self._source_uncertain = True
            raise
        self._cursors.remove(cursor)
        return True

    def _close_all_cursors(self) -> bool:
        released = True
        for cursor in list(self._cursors):
            if not self._drop_cursor(cursor):
                released = False
        return released

    def _decode(self, value: bytes) -> Any:
        # Bytes are stored verbatim by auto-pack; reading invokes the native
        # header/encoding decoder. No source-file handle reaches KohakuVault.
        self._decoder[b"value"] = value
        return self._decoder[b"value"]

    def get(self, table: str, key: str, default: Any = None) -> Any:
        if table not in _TABLES:
            raise ValueError(f"unsupported session table: {table}")
        if self._closed:
            self._raise_closed()
        if self._live is not None:
            return self._live_get(table, key, default)
        if self._db is None or table not in self._tables:
            return default
        with self._query(
            f'SELECT value FROM "{table}" WHERE key = ?', (key.encode(),)
        ) as cursor:
            row = cursor.fetchone()
        if row is not None:
            try:
                return self._decode(row[0])
            except Exception as exc:
                logger.warning(
                    "Unreadable session value", table=table, key=key, error=str(exc)
                )
        return default

    def items(self, table: str, *, prefix: str = "") -> Iterator[tuple[str, Any]]:
        """Yield decoded rows from an open view."""
        if table not in _TABLES:
            raise ValueError(f"unsupported session table: {table}")
        if self._closed:
            self._raise_closed()
        if self._live is not None:
            yield from self._live_items(table, prefix)
            return
        if self._db is None or table not in self._tables:
            return
        lower = prefix.encode()
        with self._query(
            f'SELECT key, value FROM "{table}" WHERE key >= ? AND key < ? ORDER BY key',
            (lower, lower + b"\xff"),
        ) as rows:
            for key, value in rows:
                if self._closed:
                    return
                name = key.decode("utf-8", errors="replace")
                try:
                    decoded = self._decode(value)
                except Exception as exc:
                    logger.warning(
                        "Unreadable session value",
                        table=table,
                        key=name,
                        error=str(exc),
                    )
                    continue
                yield name, decoded

    def load_meta(self, *, discover_agents: bool = True) -> dict[str, Any]:
        meta = dict(self.items("meta"))
        known = list(meta.get("agents") or [])
        if discover_agents:
            for raw_key in self._event_keys():
                parts = raw_key.rsplit(":e", 1)
                if len(parts) != 2:
                    continue
                agent = parts[0]
                if (
                    agent != "terrarium"
                    and ":attached:" not in agent
                    and agent not in known
                ):
                    known.append(agent)
        meta["agents"] = known
        return meta

    def _event_keys(self) -> Iterator[str]:
        """Event keys in order; namespace discovery reads keys, never payloads."""
        if self._closed:
            self._raise_closed()
        if self._live is not None:
            yield from self._live_keys("events", "")
            return
        if self._db is None or "events" not in self._tables:
            return
        with self._query("SELECT key FROM events ORDER BY key") as rows:
            for (raw_key,) in rows:
                if self._closed:
                    return
                yield raw_key.decode("utf-8", errors="replace")

    def _live_keys(self, table: str, prefix: str) -> list[str]:
        vault = getattr(self._live, table)
        vault.flush_cache()
        return sorted(_text(key) for key in iter_kv_keys(vault, prefix=prefix))

    def _live_get(self, table: str, key: str, default: Any) -> Any:
        try:
            return getattr(self._live, table)[key]
        except KeyError:
            return default
        except Exception as exc:
            logger.warning(
                "Unreadable session value", table=table, key=key, error=str(exc)
            )
            return default

    def _live_items(self, table: str, prefix: str) -> Iterator[tuple[str, Any]]:
        for name in self._live_keys(table, prefix):
            if self._closed:
                return
            try:
                yield name, getattr(self._live, table)[name]
            except Exception as exc:
                logger.warning(
                    "Unreadable session value", table=table, key=name, error=str(exc)
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._live = None
        source_error: BaseException | None = None
        decoder_error: BaseException | None = None
        source_released = not self._source_open_attempted
        try:
            source_released = self._dispose_source()
        except BaseException as exc:
            source_error = exc
            source_released = False
        try:
            self._dispose_decoder()
        except BaseException as exc:
            decoder_error = exc
        self._finish_lease(source_released)
        if source_error is not None:
            if decoder_error is not None:
                raise source_error from decoder_error
            raise source_error
        if decoder_error is not None:
            raise decoder_error

    def _dispose_source(self) -> bool:
        if not self._source_open_attempted:
            self._cursors.clear()
            self._db = None
            return True
        released = not self._source_uncertain
        primary: BaseException | None = None
        try:
            if not self._close_all_cursors():
                released = False
        except BaseException as exc:
            released = False
            primary = exc
        db = self._db
        self._db = None
        if db is None:
            released = False
        else:
            try:
                db.close()
            except BaseException as exc:
                released = False
                if primary is None:
                    primary = exc
                else:
                    raise primary from exc
        if primary is not None:
            raise primary
        return released

    def _dispose_decoder(self) -> None:
        decoder = self._decoder
        self._decoder = None
        if decoder is not None:
            decoder.close()

    def _finish_lease(self, source_released: bool) -> None:
        lease = self._resource_lease
        if lease is None:
            return
        self._resource_lease = None
        lease.finish(source_released=bool(source_released))

    def __enter__(self) -> "SessionReadView":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is not None:
                raise exc.with_traceback(tb) from cleanup
            raise
