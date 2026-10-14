"""Controlled metadata SQL reads with explicit, default-off admission."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kohakuvault import KVault

from kohakuterrarium.session.readonly import read_session_meta
from kohakuterrarium.session.resource_gate import ResourceLease, try_fast
from kohakuterrarium.utils.fs_path import coerce_fs_path


class MetadataReadError(Exception):
    """A failed candidate read, including primary and independent cleanup evidence."""

    def __init__(
        self,
        stage: str,
        cause: BaseException,
        cleanup_errors: tuple[BaseException, ...] = (),
    ) -> None:
        self.stage = stage
        self.cleanup_errors = cleanup_errors
        super().__init__(f"Session metadata candidate failed during {stage}")
        self.__cause__ = cause


@dataclass(frozen=True, slots=True)
class MetadataRead:
    """Materialized metadata and the admission reason selecting its read path."""

    meta: dict[str, Any]
    reason: str


def read_metadata_candidate(
    path: str | Path, *, eligible: bool = False
) -> MetadataRead:
    """Use admitted SQL or select the legacy copy path before source opening."""
    source = coerce_fs_path(path).expanduser().resolve(strict=True)
    admission = try_fast(source, eligible=eligible)
    if admission.lease is None:
        return MetadataRead(read_session_meta(source), admission.reason)
    rows = _load_source_rows(source, admission.lease)
    return MetadataRead(_decode_rows(rows), admission.reason)


def _fail(
    stage: str, primary: BaseException | None, cleanup: list[BaseException]
) -> None:
    if primary is None and not cleanup:
        return
    cause = primary if primary is not None else cleanup[0]
    if not isinstance(cause, Exception):
        cause.metadata_cleanup_errors = tuple(cleanup)
        raise cause
    raise MetadataReadError(stage, cause, tuple(cleanup)) from cause


def _load_source_rows(source: Path, lease: ResourceLease) -> list[tuple[Any, Any]]:
    db: sqlite3.Connection | None = None
    cursor: sqlite3.Cursor | None = None
    primary: BaseException | None = None
    cleanup: list[BaseException] = []
    released = False
    rows = []
    try:
        try:
            db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            cursor = db.cursor()
            cursor.execute("SELECT key, value FROM meta")
            rows = cursor.fetchall()
        except BaseException as exc:
            primary = exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException as exc:
                    cleanup.append(exc)
            if db is not None:
                try:
                    db.close()
                    released = not cleanup
                except BaseException as exc:
                    cleanup.append(exc)
        _fail("sql" if released else "resource", primary, cleanup)
        return rows
    finally:
        lease.finish(source_released=released)


def _decode_rows(rows: list[tuple[Any, Any]]) -> dict[str, Any]:
    decoder = None
    primary: BaseException | None = None
    cleanup: list[BaseException] = []
    meta = {}
    try:
        decoder = KVault(":memory:", enable_wal=False)
        decoder.enable_auto_pack()
        for key, value in rows:
            if isinstance(key, bytes):
                name = key.decode("utf-8")
            elif isinstance(key, str):
                name = key
            else:
                raise ValueError("Metadata key is not UTF-8 text")
            decoder[b"value"] = value
            meta[name] = decoder[b"value"]
    except BaseException as exc:
        primary = exc
    finally:
        if decoder is not None:
            try:
                decoder.close()
            except BaseException as exc:
                cleanup.append(exc)
            try:
                del decoder._inner
            except AttributeError:
                pass
            except BaseException as exc:
                cleanup.append(exc)
    _fail("decode", primary, cleanup)
    return meta
