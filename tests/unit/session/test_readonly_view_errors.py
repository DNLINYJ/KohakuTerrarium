"""Selective-query failures preserve primary exceptions through cleanup."""

import sqlite3
from contextlib import closing

import pytest

from kohakuterrarium.session import readonly_view, store_fork
from kohakuterrarium.session.readonly_view import SessionReadView
from kohakuterrarium.session.resource_gate import GATE, try_fast
from kohakuterrarium.session.store import SessionStore


def test_tracking_failure_survives_cursor_close(tmp_path, monkeypatch):
    path = tmp_path / "query-error.kohakutr"
    with closing(SessionStore(path)) as store:
        store.init_meta("sid", "agent", "", "", [])
    primary = RuntimeError("primary query failure")
    cleanup = sqlite3.OperationalError("cleanup failure")
    native_cursors = []
    real_connect = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def close(self):
            raise cleanup

    class Connection(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.startswith("SELECT value"):
                cursor = self.cursor(factory=Cursor)
                native_cursors.append(cursor)
                return cursor.execute(sql, params)
            return super().execute(sql, params)

    class RejectAppend(list):
        def append(self, cursor):
            raise primary

    def connect(*args, **kwargs):
        return real_connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(readonly_view.sqlite3, "connect", connect)
    view = SessionReadView(path)
    view._cursors = RejectAppend(view._cursors)
    try:
        with pytest.raises(BaseException) as caught:
            view.get("meta", "session_id")
        assert caught.value is primary
        assert caught.value.__cause__ is cleanup
    finally:
        for cursor in native_cursors:
            sqlite3.Cursor.close(cursor)
        view._cursors.clear()
        view.close()
    assert try_fast(path, eligible=True).reason == "legacy_release_uncertain"


def test_purge_tracking_setup_failure_has_no_occupancy(tmp_path, monkeypatch):
    path = tmp_path / "never-opened.kohakutr"

    def fail():
        raise MemoryError("tracking initialization failed")

    monkeypatch.setattr(store_fork, "_new_purge_cursors", fail)
    with pytest.raises(MemoryError):
        store_fork._purge_drive_tables(str(path))
    assert GATE.snapshot(path).legacy_active == 0
    assert not path.exists()
