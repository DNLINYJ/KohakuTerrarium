"""Bounded W1 metadata candidate: gate first, then source SQL or legacy copy."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kohakuterrarium.session import metadata_candidate as candidate_mod
from kohakuterrarium.session.metadata_candidate import (
    MetadataRead,
    MetadataReadError,
    read_metadata_candidate,
)
from kohakuterrarium.session.readonly import read_session_meta
from kohakuterrarium.session.resource_gate import (
    GATE,
    REASON_FAST_ACTIVE,
    REASON_LEGACY_ENROLLED,
    REASON_LEGACY_PENDING,
    REASON_LEGACY_RELEASE_UNCERTAIN,
    REASON_NOT_ELIGIBLE,
    REASON_OK,
    ResourceReleaseError,
    acquire_legacy,
    try_fast,
)
from kohakuterrarium.session.store import SessionStore


def _closed_session(path: Path, *, nested: bool = True) -> Path:
    with closing(SessionStore(path)) as store:
        store.init_meta("sess", "agent", "/p", "/w", ["alice"])
        if nested:
            store.meta["nested"] = {"items": [None, True, 4.5, "汉字"]}
            store.meta["raw"] = b"\x00\xff payload"
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

    monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)


class _FailingCloseConnection(sqlite3.Connection):
    instances: list[sqlite3.Connection] = []
    error = RuntimeError("db close failed")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)

    def close(self):
        raise type(self).error


def _force_close(connections) -> None:
    for conn in connections:
        try:
            sqlite3.Connection.close(conn)
        except Exception:
            pass


class TestSurface:
    def test_metadata_read_error_is_ordinary_exception(self):
        assert issubclass(MetadataReadError, Exception)
        assert MetadataReadError is not BaseException
        err = MetadataReadError("sql", RuntimeError("boom"))
        assert err.stage == "sql"
        assert err.cleanup_errors == ()
        assert isinstance(err.__cause__, RuntimeError)

    def test_read_signature_is_keyword_only_eligible(self):
        assert read_metadata_candidate.__defaults__ is None
        assert "eligible" in read_metadata_candidate.__code__.co_varnames


class TestGateBeforeSql:
    def test_ineligible_falls_back_to_legacy_without_sql(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "off.kohakutr")
        opened = []

        def boom(*args, **kwargs):
            opened.append(args)
            raise AssertionError("SQL must not open before declined fallback")

        monkeypatch.setattr(candidate_mod.sqlite3, "connect", boom)
        result = read_metadata_candidate(path)
        assert isinstance(result, MetadataRead)
        assert result.reason == REASON_NOT_ELIGIBLE
        assert result.meta["session_id"] == "sess"
        assert result.meta["nested"]["items"][-1] == "汉字"
        assert opened == []
        assert GATE.snapshot(path).registered is False

    def test_legacy_enrolled_falls_back_without_sql(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "enrolled.kohakutr")
        opened = []
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: opened.append(a)
            or (_ for _ in ()).throw(
                AssertionError("SQL must not open while legacy occupies")
            ),
        )
        lease = acquire_legacy(path, "open")
        try:
            result = read_metadata_candidate(path, eligible=True)
            assert result.reason == REASON_LEGACY_ENROLLED
            assert result.meta["session_id"] == "sess"
            assert opened == []
        finally:
            lease.finish(source_released=True)
        _assert_idle(path)

    def test_fast_active_falls_back_without_sql(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "active.kohakutr")
        occupying = try_fast(path, eligible=True)
        assert occupying.lease is not None
        opened = []
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: opened.append(a)
            or (_ for _ in ()).throw(
                AssertionError("SQL must not open while NEW occupies")
            ),
        )
        try:
            result = read_metadata_candidate(path, eligible=True)
            assert result.reason == REASON_FAST_ACTIVE
            assert result.meta["session_id"] == "sess"
            assert opened == []
        finally:
            occupying.lease.finish(source_released=True)
        _assert_idle(path)

    def test_legacy_pending_falls_back_without_sql(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "pending.kohakutr")
        occupying = try_fast(path, eligible=True)
        assert occupying.lease is not None
        opened = []
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: opened.append(a)
            or (_ for _ in ()).throw(AssertionError("SQL must not open while pending")),
        )
        monkeypatch.setattr(
            candidate_mod,
            "try_fast",
            lambda *_a, **_k: type(occupying)(None, REASON_LEGACY_PENDING),
        )
        try:
            result = read_metadata_candidate(path, eligible=True)
            assert result.reason == REASON_LEGACY_PENDING
            assert result.meta["session_id"] == "sess"
            assert opened == []
        finally:
            occupying.lease.finish(source_released=True)
        _assert_idle(path)

    def test_legacy_release_uncertain_falls_back_without_sql(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "legacy-uncertain.kohakutr")
        lease = acquire_legacy(path, "open")
        lease.finish(source_released=False)
        opened = []
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: opened.append(a)
            or (_ for _ in ()).throw(
                AssertionError("SQL must not open after legacy uncertain")
            ),
        )
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_LEGACY_RELEASE_UNCERTAIN
        assert result.meta["session_id"] == "sess"
        assert opened == []

    def test_new_uncertain_raises_without_legacy_or_sql(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "new-uncertain.kohakutr")
        occupying = try_fast(path, eligible=True)
        occupying.lease.finish(source_released=False)
        opened = []
        copied = []

        def boom_connect(*args, **kwargs):
            opened.append(args)
            raise AssertionError("SQL must not open after NEW uncertain")

        def boom_legacy(*args, **kwargs):
            copied.append(args)
            raise AssertionError("legacy copy must not run after NEW uncertain")

        monkeypatch.setattr(candidate_mod.sqlite3, "connect", boom_connect)
        monkeypatch.setattr(candidate_mod, "read_session_meta", boom_legacy)
        with pytest.raises(ResourceReleaseError):
            read_metadata_candidate(path, eligible=True)
        assert opened == [] and copied == []
        with pytest.raises(ResourceReleaseError):
            try_fast(path, eligible=True)


class TestAdmittedRead:
    def test_closed_store_reads_without_copy(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "closed.kohakutr")
        copies = []
        opened = []
        real_copy = (
            candidate_mod.shutil.copy2 if hasattr(candidate_mod, "shutil") else None
        )
        real_legacy_copy = __import__("shutil").copy2
        real_connect = sqlite3.connect

        def spy_copy(*args, **kwargs):
            copies.append(args)
            return real_legacy_copy(*args, **kwargs)

        def spy_connect(target, *args, **kwargs):
            opened.append(target)
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr("shutil.copy2", spy_copy)
        monkeypatch.setattr(candidate_mod.sqlite3, "connect", spy_connect)
        if real_copy is not None:
            monkeypatch.setattr(candidate_mod.shutil, "copy2", spy_copy)
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_OK
        assert result.meta["session_id"] == "sess"
        assert result.meta["nested"]["items"][-1] == "汉字"
        assert result.meta["raw"] == b"\x00\xff payload"
        assert copies == []
        assert len(opened) == 1
        assert "?mode=ro" in str(opened[0])
        assert "immutable" not in str(opened[0])
        _assert_idle(path)

    def test_file_uri_and_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = _closed_session(tmp_path / "s %20 #.kohakutr")
        via_uri = read_metadata_candidate(path.as_uri(), eligible=True)
        via_rel = read_metadata_candidate("s %20 #.kohakutr", eligible=True)
        assert via_uri.reason == REASON_OK
        assert via_rel.reason == REASON_OK
        assert via_uri.meta["session_id"] == via_rel.meta["session_id"] == "sess"
        _assert_idle(path)

    def test_missing_field_is_absent_not_defaulted(self, tmp_path):
        path = _closed_session(tmp_path / "absent.kohakutr")
        result = read_metadata_candidate(path, eligible=True)
        assert "missing" not in result.meta
        assert result.meta["session_id"] == "sess"
        _assert_idle(path)

    def test_missing_meta_table_is_strict_error(self, tmp_path):
        path = tmp_path / "no-meta.kohakutr"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE unrelated(x INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert caught.value.stage == "sql"
        assert caught.value.cleanup_errors == ()
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        _assert_idle(path)

    def test_empty_sqlite_missing_meta_is_error_not_empty_dict(self, tmp_path):
        path = tmp_path / "empty.kohakutr"
        sqlite3.connect(path).close()
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert caught.value.stage == "sql"
        assert caught.value.cleanup_errors == ()
        _assert_idle(path)

    def test_bad_utf8_key_is_strict_error(self, tmp_path):
        path = _closed_session(tmp_path / "bad-utf8.kohakutr")
        conn = sqlite3.connect(path)
        payload = conn.execute(
            "SELECT value FROM meta WHERE key=?", (b"session_id",)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)", (b"\xff\xfe", payload)
        )
        conn.commit()
        conn.close()
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert caught.value.stage == "decode"
        assert caught.value.cleanup_errors == ()
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)
        _assert_idle(path)

    def test_nontext_key_is_value_error(self, tmp_path):
        path = _closed_session(tmp_path / "nontext.kohakutr")
        conn = sqlite3.connect(path)
        payload = conn.execute(
            "SELECT value FROM meta WHERE key=?", (b"session_id",)
        ).fetchone()[0]
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (12345, payload))
        conn.commit()
        conn.close()
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert caught.value.stage == "decode"
        assert caught.value.cleanup_errors == ()
        cause = caught.value.__cause__
        assert isinstance(cause, ValueError)
        assert "UTF-8 text" in str(cause)
        _assert_idle(path)

    def test_query_does_not_checkpoint_or_journal(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "pragma.kohakutr")
        sqls: list[str] = []
        real_connect = sqlite3.connect

        class TrackingCursor(sqlite3.Cursor):
            def execute(self, sql, params=()):
                sqls.append(sql if isinstance(sql, str) else str(sql))
                return super().execute(sql, params)

        class Tracking(sqlite3.Connection):
            def cursor(self, *args, **kwargs):
                return super().cursor(factory=TrackingCursor)

            def execute(self, sql, params=()):
                sqls.append(sql if isinstance(sql, str) else str(sql))
                return super().execute(sql, params)

        def connect(target, *args, **kwargs):
            kwargs["factory"] = Tracking
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_OK
        joined = " ".join(sqls).lower()
        assert "wal_checkpoint" not in joined
        assert "journal_mode" not in joined
        assert "pragma" not in joined
        assert any("select key, value from meta" in s.lower() for s in sqls)
        _assert_idle(path)

    def test_missing_path_does_not_create_file(self, tmp_path):
        path = tmp_path / "missing.kohakutr"
        with pytest.raises(FileNotFoundError):
            read_metadata_candidate(path, eligible=True)
        assert not path.exists()
        assert GATE.snapshot(path).registered is False


class TestCleanup:
    def test_connect_failure_is_conservatively_uncertain(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "connect-fail.kohakutr")
        primary = sqlite3.OperationalError("connect failed without a handle")
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: (_ for _ in ()).throw(primary),
        )
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert caught.value.stage == "resource"
        assert caught.value.__cause__ is primary
        assert caught.value.cleanup_errors == ()
        snap = GATE.snapshot(path)
        assert snap.new_uncertain is True
        with pytest.raises(ResourceReleaseError):
            try_fast(path, eligible=True)

    def test_cursor_close_fault_marks_uncertain_even_if_conn_closes(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "cursor-close.kohakutr")
        closed_db = []
        cursor_error = sqlite3.ProgrammingError("cursor close failed")
        native = []

        class Cursor(sqlite3.Cursor):
            def close(self):
                raise cursor_error

        class Connection(sqlite3.Connection):
            def cursor(self, *args, **kwargs):
                cursor = super().cursor(factory=Cursor)
                native.append(cursor)
                return cursor

            def close(self):
                closed_db.append(True)
                return super().close()

        _connect_with_factory(monkeypatch, Connection)
        try:
            with pytest.raises(MetadataReadError) as caught:
                read_metadata_candidate(path, eligible=True)
            assert closed_db == [True]
            assert caught.value.stage == "resource"
            assert caught.value.__cause__ is cursor_error
            assert caught.value.cleanup_errors == (cursor_error,)
            snap = GATE.snapshot(path)
            assert snap.new_uncertain is True
            with pytest.raises(ResourceReleaseError):
                try_fast(path, eligible=True)
        finally:
            for cursor in native:
                try:
                    sqlite3.Cursor.close(cursor)
                except Exception:
                    pass

    def test_source_close_failure_is_resource_error(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "db-close.kohakutr")
        _FailingCloseConnection.instances = []
        close_error = _FailingCloseConnection.error
        _connect_with_factory(monkeypatch, _FailingCloseConnection)
        try:
            with pytest.raises(MetadataReadError) as caught:
                read_metadata_candidate(path, eligible=True)
            err = caught.value
            assert err.stage == "resource"
            assert err.__cause__ is close_error
            assert err.cleanup_errors == (close_error,)
            snap = GATE.snapshot(path)
            assert snap.new_uncertain is True
        finally:
            _force_close(_FailingCloseConnection.instances)

    def test_decoder_close_failure_does_not_quarantine_source(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "decoder-close.kohakutr")
        real_kvault = candidate_mod.KVault
        closed_db = []
        real_connect = sqlite3.connect
        close_error = RuntimeError("decoder close failed")

        class Recording(sqlite3.Connection):
            def close(self):
                closed_db.append(True)
                return super().close()

        def connect(target, *args, **kwargs):
            kwargs["factory"] = Recording
            return real_connect(target, *args, **kwargs)

        class BoomVault(real_kvault):
            def close(self):
                raise close_error

        monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
        monkeypatch.setattr(candidate_mod, "KVault", BoomVault)
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert closed_db == [True]
        assert caught.value.stage == "decode"
        assert caught.value.__cause__ is close_error
        assert caught.value.cleanup_errors == (close_error,)
        _assert_idle(path)

    def test_source_cleanup_happens_before_decode(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "order.kohakutr")
        events: list[str] = []
        real_connect = sqlite3.connect
        real_kvault = candidate_mod.KVault

        class Tracking(sqlite3.Connection):
            def close(self):
                events.append("source.close")
                return super().close()

        def connect(target, *args, **kwargs):
            kwargs["factory"] = Tracking
            return real_connect(target, *args, **kwargs)

        class TrackingVault(real_kvault):
            def __init__(self, *args, **kwargs):
                events.append("decoder.open")
                assert GATE.snapshot(path).registered is False
                super().__init__(*args, **kwargs)

            def enable_auto_pack(self):
                events.append("enable_auto_pack")
                assert GATE.snapshot(path).registered is False
                return super().enable_auto_pack()

            def __setitem__(self, key, value):
                events.append("decode")
                return super().__setitem__(key, value)

        monkeypatch.setattr(candidate_mod.sqlite3, "connect", connect)
        monkeypatch.setattr(candidate_mod, "KVault", TrackingVault)
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_OK
        assert "source.close" in events
        assert "decoder.open" in events
        assert "enable_auto_pack" in events
        assert events.index("source.close") < events.index("decoder.open")
        assert events.index("decoder.open") < events.index("enable_auto_pack")
        _assert_idle(path)

    def test_decoder_drops_inner(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "drop-inner.kohakutr")
        dropped = []
        real_kvault = candidate_mod.KVault

        class DropVault(real_kvault):
            def __delattr__(self, name):
                if name == "_inner":
                    dropped.append(True)
                return super().__delattr__(name)

        monkeypatch.setattr(candidate_mod, "KVault", DropVault)
        result = read_metadata_candidate(path, eligible=True)
        assert result.reason == REASON_OK
        assert dropped == [True]
        _assert_idle(path)

    def test_primary_sql_failure_survives_cleanup(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "primary.kohakutr")
        primary = RuntimeError("primary query failure")
        cleanup = sqlite3.OperationalError("cleanup failure")
        native = []

        class Cursor(sqlite3.Cursor):
            def fetchall(self):
                raise primary

            def fetchone(self):
                raise primary

            def __iter__(self):
                raise primary

            def close(self):
                raise cleanup

        class Connection(sqlite3.Connection):
            def cursor(self, *args, **kwargs):
                cursor = super().cursor(factory=Cursor)
                native.append(cursor)
                return cursor

        _connect_with_factory(monkeypatch, Connection)
        try:
            with pytest.raises(MetadataReadError) as caught:
                read_metadata_candidate(path, eligible=True)
            err = caught.value
            assert err.stage == "resource"
            assert err.__cause__ is primary
            assert primary.__cause__ is None
            assert err.cleanup_errors == (cleanup,)
        finally:
            for cursor in native:
                try:
                    sqlite3.Cursor.close(cursor)
                except Exception:
                    pass
        snap = GATE.snapshot(path)
        assert snap.new_uncertain is True

    def test_admitted_sql_error_does_not_copy_retry(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "no-retry.kohakutr")
        copied = []
        primary = sqlite3.OperationalError("admitted connect failed")

        def boom_legacy(*args, **kwargs):
            copied.append(args)
            raise AssertionError("must not copy-retry after admitted SQL")

        monkeypatch.setattr(candidate_mod, "read_session_meta", boom_legacy)
        monkeypatch.setattr(
            candidate_mod.sqlite3,
            "connect",
            lambda *a, **k: (_ for _ in ()).throw(primary),
        )
        with pytest.raises(MetadataReadError) as caught:
            read_metadata_candidate(path, eligible=True)
        assert copied == []
        assert caught.value.stage == "resource"
        assert caught.value.__cause__ is primary
        assert caught.value.cleanup_errors == ()
        with pytest.raises(ResourceReleaseError):
            try_fast(path, eligible=True)

    def test_no_source_content_in_logs(self, tmp_path, caplog):
        path = _closed_session(tmp_path / "nolog.kohakutr")
        caplog.set_level(logging.DEBUG)
        result = read_metadata_candidate(path, eligible=True)
        assert result.meta["session_id"] == "sess"
        text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "sess" not in text
        assert "汉字" not in text
        _assert_idle(path)


def test_legacy_fallback_uses_read_session_meta(tmp_path, monkeypatch):
    path = _closed_session(tmp_path / "legacy-spy.kohakutr")
    called = []
    original = read_session_meta

    def spy(target):
        called.append(target)
        return original(target)

    monkeypatch.setattr(candidate_mod, "read_session_meta", spy)
    result = read_metadata_candidate(path, eligible=False)
    assert called
    assert result.reason == REASON_NOT_ELIGIBLE
    assert result.meta["session_id"] == "sess"
