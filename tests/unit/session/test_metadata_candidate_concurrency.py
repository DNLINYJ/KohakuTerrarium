"""Public I/O seams: WAL visibility, NEW-first close barrier, native-after-close."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from kohakuterrarium.session import metadata_candidate as candidate_mod
from kohakuterrarium.session import store as store_mod
from kohakuterrarium.session.memory import SessionMemory
from kohakuterrarium.session.metadata_candidate import read_metadata_candidate
from kohakuterrarium.session.resource_gate import (
    GATE,
    REASON_LEGACY_ENROLLED,
    REASON_OK,
    try_fast,
)
from kohakuterrarium.session.store import SessionStore

_REAL = sqlite3.connect
_T = 5.0


def _closed(path: Path) -> Path:
    with closing(SessionStore(path)) as store:
        store.init_meta("sess", "agent", "/p", "/w", ["alice"])
        store.meta["nested"] = {"items": [None, True, 4.5, "汉字"]}
        store.meta["raw"] = b"\x00\xff payload"
    return path


def _idle(path) -> None:
    admitted = try_fast(path, eligible=True)
    assert admitted.reason == REASON_OK and admitted.lease is not None
    admitted.lease.finish(source_released=True)


def _meta_rows(path: Path) -> list[tuple]:
    conn = _REAL(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return list(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()


class _HoldClose(sqlite3.Connection):
    """Keep the real sqlite handle until the test releases close."""

    entered: threading.Event
    release: threading.Event
    closed: threading.Event
    sqls: list[str]
    _held: bool

    def cursor(self, *args, **kwargs):
        self.set_trace_callback(type(self).sqls.append)
        return super().cursor(*args, **kwargs)

    def close(self):
        cls = type(self)
        if not getattr(self, "_held", False):
            self._held = True
            cls.entered.set()
            assert cls.release.wait(_T)
        result = sqlite3.Connection.close(self)
        cls.closed.set()
        return result


def _hold(monkeypatch, entered, release, closed) -> type[_HoldClose]:
    class Hold(_HoldClose):
        pass

    Hold.entered, Hold.release, Hold.closed, Hold.sqls = entered, release, closed, []

    def connect(target, *args, **kwargs):
        kwargs["factory"] = Hold
        return _REAL(target, *args, **kwargs)

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
    return Hold


def test_mode_ro_sees_committed_wal_not_uncommitted(tmp_path, monkeypatch):
    path = _closed(tmp_path / "wal.kohakutr")
    packed = dict(_meta_rows(path))
    key = next(k for k in packed if k in (b"session_id", "session_id"))
    payload = packed[key]
    writer = _REAL(path)
    writer.execute("PRAGMA journal_mode=WAL")
    assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    writer.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (b"wal_ok", payload))
    writer.commit()
    wal = Path(f"{path}-wal")
    assert wal.exists() and wal.stat().st_size > 0
    writer.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (b"wal_hid", payload))
    main_b = path.read_bytes()
    wal_b = wal.read_bytes()
    sql_b = _meta_rows(path)
    sqls, decoder = [], []
    real_kv = candidate_mod.KVault

    class Track(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            self.set_trace_callback(sqls.append)
            return super().cursor(*args, **kwargs)

    def connect(target, *args, **kwargs):
        kwargs["factory"] = Track
        return _REAL(target, *args, **kwargs)

    class Spy(real_kv):
        def __init__(self, vault_path, *a, **k):
            decoder.append(str(vault_path))
            super().__init__(vault_path, *a, **k)

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
    monkeypatch.setattr(candidate_mod, "KVault", Spy)
    try:
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_OK
        assert result.meta["session_id"] == result.meta["wal_ok"] == "sess"
        assert result.meta["nested"]["items"][-1] == "汉字"
        assert "wal_hid" not in result.meta
        assert decoder == [":memory:"]
        joined = " ".join(sqls).lower()
        assert "select key, value from meta" in joined
        assert "wal_checkpoint" not in joined and "journal_mode" not in joined
        assert path.read_bytes() == main_b and wal.read_bytes() == wal_b
        assert _meta_rows(path) == sql_b
        assert writer.execute(
            "SELECT 1 FROM meta WHERE key=?", (b"wal_hid",)
        ).fetchone()
    finally:
        writer.rollback()
        writer.close()
    _idle(path)


def test_new_first_store_waits_unrelated_file_progresses(tmp_path, monkeypatch):
    path = _closed(tmp_path / "held.kohakutr")
    other = tmp_path / "other.kohakutr"
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    Hold = _hold(monkeypatch, entered, release, closed)
    other_done, store_ready = threading.Event(), threading.Event()
    errors: list[BaseException] = []
    stores: list[SessionStore] = []
    real_vault = store_mod.KVault

    def guarded_native(target, *args, **kwargs):
        if Path(target).resolve() == path.resolve():
            assert closed.is_set(), "source SQL must close before native opens"
        return real_vault(target, *args, **kwargs)

    monkeypatch.setattr(store_mod, "KVault", guarded_native)

    def read():
        try:
            assert (
                read_metadata_candidate(path, eligible=True).meta["session_id"]
                == "sess"
            )
        except BaseException as exc:
            errors.append(exc)

    def open_store():
        try:
            stores.append(SessionStore(path))
        except BaseException as exc:
            errors.append(exc)
        store_ready.set()

    def other_file():
        with closing(SessionStore(other)) as store:
            store.init_meta("other", "agent", "/p", "/w", ["bob"])
        other_done.set()

    worker = threading.Thread(target=read)
    waiter = threading.Thread(target=open_store)
    sibling = threading.Thread(target=other_file)
    worker.start()
    try:
        assert entered.wait(_T) and GATE.snapshot(path).new_active == 1
        waiter.start()
        sibling.start()
        deadline = time.monotonic() + _T
        while time.monotonic() < deadline and GATE.snapshot(path).legacy_pending != 1:
            time.sleep(0.01)
        assert GATE.snapshot(path).legacy_pending == 1
        assert other_done.wait(_T)
        assert not store_ready.is_set() and not closed.is_set()
    finally:
        release.set()
        worker.join(_T)
        if waiter.ident is not None:
            waiter.join(_T)
        if sibling.ident is not None:
            sibling.join(_T)
        for store in stores:
            store.close(update_status=False)
    assert not worker.is_alive() and closed.is_set() and store_ready.is_set()
    assert not errors and stores
    joined = " ".join(Hold.sqls).lower()
    assert "select key, value from meta" in joined
    assert "wal_checkpoint" not in joined and "journal_mode" not in joined
    _idle(path)
    _idle(other)


def test_cancel_to_thread_does_not_release_until_worker_finishes(tmp_path, monkeypatch):
    path = _closed(tmp_path / "cancel.kohakutr")
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    _hold(monkeypatch, entered, release, closed)
    done = threading.Event()
    errors = []

    def hold_read():
        try:
            out = read_metadata_candidate(path, eligible=True)
            assert out.reason == REASON_OK and out.meta["session_id"] == "sess"
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    async def run():
        task = asyncio.create_task(asyncio.to_thread(hold_read))
        try:
            assert await asyncio.to_thread(entered.wait, _T)
            assert GATE.snapshot(path).new_active == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.05)
            assert not done.is_set() and not closed.is_set()
            assert GATE.snapshot(path).new_active == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(done.wait, _T)
        assert closed.is_set() and not errors
        _idle(path)

    asyncio.run(run())


def test_native_starts_only_after_sql_close_fail_safe_bounded(tmp_path, monkeypatch):
    path = _closed(tmp_path / "order.kohakutr")
    sql_closed = threading.Event()
    order, native_paths = [], []
    real_kv = candidate_mod.KVault

    class Track(sqlite3.Connection):
        def close(self):
            result = super().close()
            order.append("sql.close")
            sql_closed.set()
            return result

    def connect(target, *args, **kwargs):
        order.append("sql.connect")
        kwargs["factory"] = Track
        return _REAL(target, *args, **kwargs)

    class Ordered(real_kv):
        def __init__(self, vault_path, *a, **k):
            native_paths.append(str(vault_path))
            assert sql_closed.is_set()
            order.append("native.open")
            super().__init__(vault_path, *a, **k)

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
    monkeypatch.setattr(candidate_mod, "KVault", Ordered)
    result = read_metadata_candidate(path, eligible=True)
    assert result.reason == REASON_OK and result.meta["session_id"] == "sess"
    assert native_paths == [":memory:"]
    assert order == ["sql.connect", "sql.close", "native.open"]
    _idle(path)

    boom = _closed(tmp_path / "boom.kohakutr")
    closed2, attempts = threading.Event(), []

    class Track2(sqlite3.Connection):
        def close(self):
            result = super().close()
            closed2.set()
            return result

    def connect2(target, *args, **kwargs):
        kwargs["factory"] = Track2
        return _REAL(target, *args, **kwargs)

    class Boom(real_kv):
        def __init__(self, vault_path, *a, **k):
            attempts.append(time.monotonic())
            assert closed2.is_set() and str(vault_path) == ":memory:"
            raise RuntimeError("decoder open failed")

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect2)
    monkeypatch.setattr(candidate_mod, "KVault", Boom)
    started = time.monotonic()
    with pytest.raises(Exception):
        read_metadata_candidate(boom, eligible=True)
    assert attempts and closed2.is_set() and time.monotonic() - started < 2.0
    _idle(boom)


def test_legacy_store_and_memory_fallback_without_python_sql(tmp_path, monkeypatch):
    path = tmp_path / "live.kohakutr"
    opened: list[object] = []

    def boom(*args, **kwargs):
        opened.append(args)
        raise AssertionError("Python SQL must not open while legacy occupies")

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", boom)
    store = SessionStore(path)
    memory = SessionMemory(str(path))
    try:
        store.init_meta("sess", "agent", "/p", "/w", ["alice"])
        store.meta["nested"] = {"items": [None, True, 4.5, "汉字"]}
        store.meta["raw"] = b"\x00\xff payload"
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_LEGACY_ENROLLED
        assert result.meta["session_id"] == "sess"
        assert result.meta["nested"]["items"][-1] == "汉字"
        assert result.meta["raw"] == b"\x00\xff payload"
        assert opened == [] and GATE.snapshot(path).legacy_active >= 1
    finally:
        memory.close()
        store.close(update_status=False)
    _idle(path)
