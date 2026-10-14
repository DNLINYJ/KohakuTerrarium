"""Interrupted and multiply failing candidate reads keep lifecycle evidence."""

import sqlite3
from contextlib import closing

import pytest

from kohakuterrarium.session import metadata_candidate as candidate
from kohakuterrarium.session.resource_gate import GATE
from kohakuterrarium.session.store import SessionStore


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "interrupt.kohakutr"
    with closing(SessionStore(path)) as store:
        store.init_meta("sid", "agent", "", "", ["alice"])
    return path


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_query_interrupt_preserved_and_db_close_attempted(
    source, monkeypatch, cleanup_failure
):
    primary = KeyboardInterrupt("query interrupted")
    cleanup = OSError("cursor cleanup")
    closed, cursors, connections = [], [], []
    original = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def fetchall(self):
            raise primary

        def close(self):
            if cleanup_failure:
                raise cleanup
            super().close()

    class Connection(sqlite3.Connection):
        def cursor(self):
            cursor = super().cursor(factory=Cursor)
            cursors.append(cursor)
            return cursor

        def close(self):
            super().close()
            closed.append(True)

    def connect(*args, **kwargs):
        conn = original(*args, factory=Connection, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(candidate.sqlite3, "connect", connect)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            candidate.read_metadata_candidate(source, eligible=True)
        assert caught.value is primary
        assert primary.metadata_cleanup_errors == (
            (cleanup,) if cleanup_failure else ()
        )
        assert closed == [True]
        assert GATE.snapshot(source).new_uncertain == cleanup_failure
    finally:
        for cursor in cursors:
            try:
                sqlite3.Cursor.close(cursor)
            except sqlite3.ProgrammingError:
                pass
        for conn in connections:
            sqlite3.Connection.close(conn)


def test_decoder_interrupt_and_two_cleanup_faults_preserve_evidence(
    source, monkeypatch
):
    real_decoder = candidate.KVault
    primary = KeyboardInterrupt("decode interrupted")
    close_error, drop_error = OSError("close"), OSError("drop")
    handles = []

    class Decoder(real_decoder):
        def enable_auto_pack(self):
            handles.append(self)
            raise primary

        def close(self):
            raise close_error

        def __delattr__(self, name):
            if name == "_inner":
                raise drop_error
            super().__delattr__(name)

    monkeypatch.setattr(candidate, "KVault", Decoder)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            candidate.read_metadata_candidate(source, eligible=True)
        assert caught.value is primary
        assert primary.metadata_cleanup_errors == (close_error, drop_error)
        assert not GATE.snapshot(source).registered
    finally:
        for decoder in handles:
            real_decoder.close(decoder)
            object.__delattr__(decoder, "_inner")


def test_query_execution_failure_still_closes_explicit_cursor(source, monkeypatch):
    original = sqlite3.connect
    order = []
    failure = sqlite3.OperationalError("execute failed")

    class Cursor(sqlite3.Cursor):
        def execute(self, *args):
            raise failure

        def close(self):
            order.append("cursor")
            super().close()

    class Connection(sqlite3.Connection):
        def cursor(self):
            return super().cursor(factory=Cursor)

        def close(self):
            order.append("database")
            super().close()

    monkeypatch.setattr(
        candidate.sqlite3,
        "connect",
        lambda *a, **kw: original(*a, factory=Connection, **kw),
    )
    with pytest.raises(candidate.MetadataReadError) as caught:
        candidate.read_metadata_candidate(source, eligible=True)
    assert caught.value.stage == "sql"
    assert caught.value.__cause__ is failure
    assert order == ["cursor", "database"]
    assert not GATE.snapshot(source).registered
