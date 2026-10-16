"""Read-view edges: corrupt payloads, missing tables, stale generators, cleanup."""

import logging
import sqlite3
from contextlib import closing

import pytest

from kohakuterrarium.session import readonly_view
from kohakuterrarium.session.readonly_view import SessionReadView
from kohakuterrarium.session.resource_gate import (
    REASON_NOT_ELIGIBLE,
    REASON_OK,
    REASON_LEGACY_RELEASE_UNCERTAIN as UNCERTAIN,
    try_fast,
)
from kohakuterrarium.session.store import SessionStore


def _populate(store):
    store.init_meta("sid", "agent", "", "", ["alice"])
    store.meta["nested"] = {"items": [None, True, 4.5, "汉字"]}
    store.append_event("alice", "user_input", {"content": "first"})
    store.append_event("alice", "user_input", {"content": "second"})
    store.append_event("discovered", "user_input", {"content": "new agent"})


def _closed_session(path):
    with closing(SessionStore(path)) as store:
        _populate(store)
    return path


def _assert_idle(path) -> None:
    admitted = try_fast(path, eligible=True)
    assert admitted.reason == REASON_OK
    assert admitted.lease is not None
    admitted.lease.finish(source_released=True)


def _connect_with_factory(monkeypatch, factory):
    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        kwargs["factory"] = factory
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(readonly_view.sqlite3, "connect", connect)


def _force_close(connections) -> None:
    for conn in connections:
        try:
            sqlite3.Connection.close(conn)
        except Exception:
            pass


def _corrupt_packed(tmp_path, value) -> bytes:
    side = tmp_path / "_pack.kohakutr"
    with closing(SessionStore(side)) as store:
        store.meta["payload"] = value
    conn = sqlite3.connect(side)
    try:
        payload = conn.execute(
            "SELECT value FROM meta WHERE key=?", (b"payload",)
        ).fetchone()[0]
    finally:
        conn.close()
    mutated = bytearray(payload)
    mutated[2] ^= 0x7F
    return bytes(mutated)


def _unreadable(caplog):
    return [
        rec for rec in caplog.records if rec.getMessage() == "Unreadable session value"
    ]


class _FailingDbClose(sqlite3.Connection):
    instances: list[sqlite3.Connection] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)

    def close(self):
        raise RuntimeError("db close failed")


def test_empty_sqlite_returns_defaults_and_leaves_new_off(tmp_path):
    path = tmp_path / "empty.kohakutr"
    sqlite3.connect(path).close()
    with SessionReadView(path) as reader:
        assert reader.get("meta", "nested", "DEFAULT") == "DEFAULT"
        assert list(reader.items("events")) == []
        assert reader.load_meta() == {"agents": []}
    assert try_fast(path).reason == REASON_NOT_ELIGIBLE
    _assert_idle(path)


def test_valid_sqlite_missing_session_tables_returns_defaults(tmp_path):
    path = tmp_path / "unrelated.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated(x INTEGER)")
    conn.commit()
    conn.close()
    with SessionReadView(path) as reader:
        assert reader.get("state", "missing", "DEFAULT") == "DEFAULT"
        assert list(reader.items("conversation")) == []
        assert reader.load_meta() == {"agents": []}
    _assert_idle(path)


def test_snapshot_corrupt_payload_get_and_items_log_and_skip(tmp_path, caplog):
    path = _closed_session(tmp_path / "corrupt.kohakutr")
    conn = sqlite3.connect(path)
    nested = conn.execute(
        "SELECT value FROM meta WHERE key=?", (b"nested",)
    ).fetchone()[0]
    event = conn.execute(
        "SELECT key, value FROM events WHERE key=?", (b"alice:e000000",)
    ).fetchone()
    broken_nested = bytearray(nested)
    broken_nested[2] ^= 0x7F
    broken_event = bytearray(event[1])
    broken_event[2] ^= 0x7F
    conn.execute(
        "UPDATE meta SET value=? WHERE key=?", (bytes(broken_nested), b"nested")
    )
    conn.execute(
        "UPDATE events SET value=? WHERE key=?", (bytes(broken_event), event[0])
    )
    conn.commit()
    conn.close()
    caplog.set_level(logging.WARNING, logger="kohakuterrarium.session.readonly_view")
    with SessionReadView(path) as reader:
        assert reader.get("meta", "nested", "DEFAULT") == "DEFAULT"
        items = list(reader.items("events"))
        assert [key for key, _ in items] == ["alice:e000001", "discovered:e000000"]
        assert items[0][1]["content"] == "second"
        assert reader.get("meta", "session_id") == "sid"
    records = _unreadable(caplog)
    assert {getattr(rec, "table", None) for rec in records} == {"meta", "events"}
    assert {getattr(rec, "key", None) for rec in records} == {
        "nested",
        "alice:e000000",
    }
    assert all(
        "Unsupported header version" in getattr(rec, "error", "") for rec in records
    )
    _assert_idle(path)


def test_load_meta_skips_event_keys_without_namespace_marker(tmp_path):
    path = _closed_session(tmp_path / "orphan.kohakutr")
    conn = sqlite3.connect(path)
    payload = conn.execute("SELECT value FROM events LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO events (key, value) VALUES (?, ?)", (b"orphan", payload))
    conn.commit()
    conn.close()
    with SessionReadView(path) as reader:
        assert reader.load_meta()["agents"] == ["alice", "discovered"]
    _assert_idle(path)


def test_live_corrupt_kv_bytes_get_and_items_log(tmp_path, caplog):
    path = tmp_path / "live-corrupt.kohakutr"
    corrupt = _corrupt_packed(tmp_path, {"ok": 1})
    store = SessionStore(path)
    try:
        _populate(store)
        store.meta.put(b"badver", corrupt)
        store.events.put(b"alice:e000050", corrupt)
        caplog.set_level(
            logging.WARNING, logger="kohakuterrarium.session.readonly_view"
        )
        with SessionReadView(path) as reader:
            assert reader.get("meta", "badver", "DEFAULT") == "DEFAULT"
            assert reader.get("meta", "nested")["items"][-1] == "汉字"
            items = list(reader.items("events", prefix="alice:e"))
            assert [key for key, _ in items] == ["alice:e000000", "alice:e000001"]
            assert items[0][1]["content"] == "first"
        records = _unreadable(caplog)
        assert {getattr(rec, "key", None) for rec in records} == {
            "badver",
            "alice:e000050",
        }
    finally:
        store.close()
    _assert_idle(path)


def test_stale_snapshot_generator_does_not_reopen_new(tmp_path):
    path = _closed_session(tmp_path / "stale-snap.kohakutr")
    reader = SessionReadView(path)
    events = reader.items("events")
    keys = reader._event_keys()
    assert next(events)[0] == "alice:e000000"
    assert next(keys) == "alice:e000000"
    reader.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        next(events)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        next(keys)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        reader.get("meta", "nested")
    assert try_fast(path).reason == REASON_NOT_ELIGIBLE
    _assert_idle(path)


def test_stale_live_generator_stops_without_closing_store(tmp_path):
    path = tmp_path / "stale-live.kohakutr"
    store = SessionStore(path)
    try:
        _populate(store)
        reader = SessionReadView(path)
        events = reader.items("events")
        assert next(events)[0] == "alice:e000000"
        reader.close()
        assert list(events) == []
        assert store.get_events("alice")[0]["content"] == "first"
    finally:
        store.close()
    _assert_idle(path)


def test_marked_closed_snapshot_generators_stop_without_reopen(tmp_path):
    path = _closed_session(tmp_path / "flag.kohakutr")
    reader = SessionReadView(path)
    events = reader.items("events")
    keys = reader._event_keys()
    assert next(events)[0] == "alice:e000000"
    assert next(keys) == "alice:e000000"
    reader._closed = True
    assert list(events) == []
    assert list(keys) == []
    reader._closed = False
    reader.close()
    assert try_fast(path).reason == REASON_NOT_ELIGIBLE
    _assert_idle(path)


@pytest.mark.parametrize("phase", ["get", "items"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_query_primary_survives_cursor_cleanup(
    tmp_path, monkeypatch, phase, cleanup_fails
):
    path = _closed_session(tmp_path / f"query-{phase}-{cleanup_fails}.kohakutr")
    primary = RuntimeError("primary query failure")
    cleanup = sqlite3.OperationalError("cleanup failure")
    native = []
    match = "SELECT value FROM" if phase == "get" else "SELECT key, value FROM"

    class Cursor(sqlite3.Cursor):
        def fetchone(self):
            raise primary

        def __next__(self):
            raise primary

        def close(self):
            if cleanup_fails:
                raise cleanup
            return super().close()

    class Connection(sqlite3.Connection):
        def execute(self, sql, params=()):
            if isinstance(sql, str) and match in sql:
                cursor = self.cursor(factory=Cursor)
                native.append(cursor)
                return cursor.execute(sql, params)
            return super().execute(sql, params)

    _connect_with_factory(monkeypatch, Connection)
    view = SessionReadView(path)
    try:
        with pytest.raises(RuntimeError, match="primary query failure") as caught:
            if phase == "get":
                view.get("meta", "session_id")
            else:
                list(view.items("events"))
        assert caught.value is primary
        if cleanup_fails:
            assert caught.value.__cause__ is cleanup
        else:
            assert caught.value.__cause__ is None
    finally:
        if cleanup_fails:
            for cursor in native:
                try:
                    sqlite3.Cursor.close(cursor)
                except Exception:
                    pass
            view._cursors.clear()
        view.close()
    if cleanup_fails:
        assert try_fast(path, eligible=True).reason == UNCERTAIN
    else:
        _assert_idle(path)


def test_execute_closed_before_tracking_keeps_source_uncertain(tmp_path, monkeypatch):
    path = _closed_session(tmp_path / "track-closed.kohakutr")
    holder = {}

    class Connection(sqlite3.Connection):
        def execute(self, sql, params=()):
            cursor = super().execute(sql, params)
            view = holder.get("view")
            if view is not None and isinstance(sql, str) and "SELECT value FROM" in sql:
                view._closed = True
            return cursor

    _connect_with_factory(monkeypatch, Connection)
    view = SessionReadView(path)
    holder["view"] = view
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            view.get("meta", "nested")
    finally:
        view._closed = False
        view.close()
    assert try_fast(path, eligible=True).reason == UNCERTAIN


def test_drop_cursor_ignores_unknown_handles(tmp_path):
    path = _closed_session(tmp_path / "drop.kohakutr")
    reader = SessionReadView(path)
    assert reader._drop_cursor(None) is True
    assert reader._drop_cursor(object()) is True
    reader.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        reader._execute("SELECT 1")
    _assert_idle(path)


def test_programmingerror_on_active_cursor_close_disables_new(tmp_path, monkeypatch):
    path = _closed_session(tmp_path / "prog-close.kohakutr")

    class Cursor(sqlite3.Cursor):
        def close(self):
            if getattr(self, "_fail", False):
                raise sqlite3.ProgrammingError("already closed")
            return super().close()

    class Connection(sqlite3.Connection):
        def execute(self, sql, params=()):
            cursor = super().cursor(Cursor)
            cursor.execute(sql, params)
            if isinstance(sql, str) and "SELECT key, value FROM" in sql:
                cursor._fail = True
            return cursor

    _connect_with_factory(monkeypatch, Connection)
    reader = SessionReadView(path)
    gen = reader.items("events")
    assert next(gen)[0] == "alice:e000000"
    reader.close()
    assert try_fast(path, eligible=True).reason == UNCERTAIN


def test_cursor_and_db_close_failures_chain_primary(tmp_path, monkeypatch):
    path = _closed_session(tmp_path / "both-source.kohakutr")
    _FailingDbClose.instances = []

    class Cursor(sqlite3.Cursor):
        def close(self):
            if getattr(self, "_fail", False):
                raise RuntimeError("cursor close boom")
            return super().close()

    class Connection(_FailingDbClose):
        def execute(self, sql, params=()):
            cursor = super().cursor(Cursor)
            cursor.execute(sql, params)
            if isinstance(sql, str) and "SELECT key, value FROM" in sql:
                cursor._fail = True
            return cursor

    _connect_with_factory(monkeypatch, Connection)
    reader = SessionReadView(path)
    gen = reader.items("events")
    assert next(gen)[0] == "alice:e000000"
    try:
        with pytest.raises(RuntimeError, match="cursor close boom") as caught:
            reader.close()
        assert "db close failed" in str(caught.value.__cause__)
        assert try_fast(path, eligible=True).reason == UNCERTAIN
    finally:
        _force_close(_FailingDbClose.instances)


def test_db_and_decoder_close_failures_chain_primary(tmp_path, monkeypatch):
    path = _closed_session(tmp_path / "db-decoder.kohakutr")
    _FailingDbClose.instances = []
    real_kvault = readonly_view.KVault

    class BoomVault(real_kvault):
        def close(self):
            raise RuntimeError("decoder close failed")

    _connect_with_factory(monkeypatch, _FailingDbClose)
    monkeypatch.setattr(readonly_view, "KVault", BoomVault)
    reader = SessionReadView(path)
    try:
        with pytest.raises(RuntimeError, match="db close failed") as caught:
            reader.close()
        assert "decoder close failed" in str(caught.value.__cause__)
        reader.close()
        assert try_fast(path, eligible=True).reason == UNCERTAIN
    finally:
        _force_close(_FailingDbClose.instances)


def test_finish_lease_after_close_is_idempotent(tmp_path):
    path = _closed_session(tmp_path / "lease.kohakutr")
    reader = SessionReadView(path)
    reader.close()
    reader._finish_lease(True)
    reader._finish_lease(False)
    reader.close()
    assert try_fast(path).reason == REASON_NOT_ELIGIBLE
    _assert_idle(path)
