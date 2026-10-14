"""Candidate source-release and detached decoder failure contracts."""

import sqlite3
from contextlib import closing

import pytest

from kohakuterrarium.session import metadata_candidate as candidate
from kohakuterrarium.session.resource_gate import (
    GATE,
    ResourceReleaseError,
    acquire_legacy,
)
from kohakuterrarium.session.store import SessionStore


def saved(path):
    with closing(SessionStore(path)) as store:
        store.init_meta("sid", "agent", "", "", ["alice"])
    return path


def test_decoder_setup_failure_disposes_inner_after_source_release(
    tmp_path, monkeypatch
):
    path = saved(tmp_path / "decoder.kohakutr")
    original = candidate.KVault
    opened = []
    primary = RuntimeError("decoder configuration failed")

    class Decoder(original):
        def enable_auto_pack(self):
            opened.append(self)
            assert not GATE.snapshot(path).registered
            raise primary

    monkeypatch.setattr(candidate, "KVault", Decoder)
    try:
        with pytest.raises(candidate.MetadataReadError) as caught:
            candidate.read_metadata_candidate(path, eligible=True)
        assert caught.value.__cause__ is primary
        assert opened
        assert not hasattr(opened[0], "_inner")
        assert not GATE.snapshot(path).registered
    finally:
        for decoder in opened:
            if hasattr(decoder, "_inner"):
                decoder.close()
                del decoder._inner


def test_query_failure_retains_existing_cause_and_all_cleanup_errors(
    tmp_path, monkeypatch
):
    path = saved(tmp_path / "query.kohakutr")
    root = ValueError("query root")
    primary = sqlite3.OperationalError("query failed")
    primary.__cause__ = root
    cursor_error = RuntimeError("cursor close")
    db_error = RuntimeError("database close")
    connections, cursors, attempts = [], [], []
    original = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def fetchall(self):
            raise primary

        def close(self):
            attempts.append("cursor")
            raise cursor_error

    class Connection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            cursor = super().cursor(factory=Cursor)
            cursors.append(cursor)
            return cursor

        def execute(self, sql, params=()):
            return self.cursor().execute(sql, params)

        def close(self):
            attempts.append("database")
            raise db_error

    def connect(*args, **kwargs):
        connection = original(*args, factory=Connection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(candidate.sqlite3, "connect", connect)
    try:
        with pytest.raises(candidate.MetadataReadError) as caught:
            candidate.read_metadata_candidate(path, eligible=True)
        assert attempts == ["cursor", "database"]
        assert caught.value.stage == "resource"
        assert caught.value.__cause__ is primary
        assert primary.__cause__ is root
        assert caught.value.cleanup_errors == (cursor_error, db_error)
        with pytest.raises(ResourceReleaseError):
            acquire_legacy(path, "must decline")
    finally:
        for cursor in cursors:
            sqlite3.Cursor.close(cursor)
        for connection in connections:
            sqlite3.Connection.close(connection)


def test_decoder_only_close_failure_does_not_hold_source(tmp_path, monkeypatch):
    path = saved(tmp_path / "private.kohakutr")
    original = candidate.KVault
    failure = RuntimeError("decoder disposal")
    opened = []

    class Decoder(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def close(self):
            assert not GATE.snapshot(path).registered
            raise failure

    monkeypatch.setattr(candidate, "KVault", Decoder)
    with pytest.raises(candidate.MetadataReadError) as caught:
        candidate.read_metadata_candidate(path, eligible=True)
    assert caught.value.stage == "decode"
    assert caught.value.__cause__ is failure
    assert not hasattr(opened[0], "_inner")
    assert not GATE.snapshot(path).registered
