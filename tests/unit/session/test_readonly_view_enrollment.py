"""SessionReadView occupancy spans snapshot SQL and borrowed live views."""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from kohakuterrarium.session import readonly_view
from kohakuterrarium.session.memory import SessionMemory
from kohakuterrarium.session.open_registry import live_store_for
from kohakuterrarium.session.readonly_view import SessionReadView
from kohakuterrarium.session.resource_gate import (
    GATE,
    REASON_LEGACY_ENROLLED,
    REASON_LEGACY_PENDING,
    REASON_LEGACY_RELEASE_UNCERTAIN as UNCERTAIN,
    REASON_OK,
    ResourceReleaseError,
    try_fast,
)
from kohakuterrarium.session.store import SessionStore
from tests.unit.session.test_readonly_view import _ForbiddenSqlite, _populate


def _closed_session(path):
    with closing(SessionStore(path)) as store:
        _populate(store)
    return path


def _wait_pending(path, count: int = 1, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if GATE.snapshot(path).legacy_pending == count:
            return
        time.sleep(0.01)
    raise AssertionError(f"pending occupancy did not reach {count}")


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


def _force_close_failing():
    for conn in _FailingCloseConnection.instances:
        try:
            conn.force_close()
        except Exception:
            pass


class _FailingCloseConnection(sqlite3.Connection):
    instances: list[sqlite3.Connection] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)

    def close(self):
        raise RuntimeError("db close failed")

    def force_close(self):
        super().close()


class _TrackingConnection(sqlite3.Connection):
    events: list = []

    def execute(self, *args, **kwargs):
        type(self).events.append(("execute", args[0] if args else None))
        return super().cursor(_TrackingCursor).execute(*args, **kwargs)

    def close(self):
        type(self).events.append("db.close")
        return super().close()


class _TrackingCursor(sqlite3.Cursor):
    def close(self):
        _TrackingConnection.events.append("cursor.close")
        return super().close()


class _FailGetCloseCursor(sqlite3.Cursor):
    def close(self):
        if getattr(self, "_fail_close", False):
            raise sqlite3.ProgrammingError("get cursor close failed")
        return super().close()


class _GetCursorCloseFails(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        cursor = super().cursor(_FailGetCloseCursor)
        cursor.execute(*args, **kwargs)
        sql = args[0] if args else ""
        if isinstance(sql, str) and "SELECT value FROM" in sql:
            cursor._fail_close = True
        return cursor


class TestReadViewEnrollment:
    def test_snapshot_enrolls_legacy_and_declines_new(self, tmp_path):
        path = _closed_session(tmp_path / "enroll.kohakutr")
        with SessionReadView(path) as reader:
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 1 and snap.legacy_pending == 0
            denied = try_fast(path, eligible=True)
            assert denied.lease is None and denied.reason == REASON_LEGACY_ENROLLED
            assert reader.get("meta", "nested")["items"][-1] == "汉字"
        _assert_idle(path)

    def test_live_view_enrolls_without_opening_sql(self, tmp_path, monkeypatch):
        path = tmp_path / "live-enroll.kohakutr"
        with closing(SessionStore(path)) as store:
            _populate(store)
            monkeypatch.setattr(readonly_view, "sqlite3", _ForbiddenSqlite())
            with SessionReadView(path) as reader:
                assert GATE.snapshot(path).legacy_active == 2
                assert try_fast(path, eligible=True).reason == REASON_LEGACY_ENROLLED
                assert reader.get("meta", "session_id") == "sid"
            assert GATE.snapshot(path).legacy_active == 1
            assert store.is_ready
        _assert_idle(path)

    def test_legacy_store_and_view_still_nest(self, tmp_path):
        path = tmp_path / "nest.kohakutr"
        store = SessionStore(path)
        try:
            _populate(store)
            reader = SessionReadView(path)
            try:
                assert GATE.snapshot(path).legacy_active == 2
                assert try_fast(path, eligible=True).reason == REASON_LEGACY_ENROLLED
            finally:
                reader.close()
            assert GATE.snapshot(path).legacy_active == 1
        finally:
            store.close()
        _assert_idle(path)

    def test_view_waits_without_sql_until_new_releases(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "wait.kohakutr")
        occupying = try_fast(path, eligible=True)
        assert occupying.lease is not None
        opened, live_calls, errors = [], [], []
        real_connect, real_live = sqlite3.connect, readonly_view.live_store_for

        def spy_connect(target, *a, **k):
            opened.append(target)
            return real_connect(target, *a, **k)

        def spy_live(target):
            live_calls.append(target)
            return real_live(target)

        monkeypatch.setattr(readonly_view.sqlite3, "connect", spy_connect)
        monkeypatch.setattr(readonly_view, "live_store_for", spy_live)
        ready, release = threading.Event(), threading.Event()

        def open_view():
            try:
                view = SessionReadView(path)
                ready.set()
                assert release.wait(5)
                view.close()
            except BaseException as exc:
                errors.append(exc)
                ready.set()
                release.set()

        worker = threading.Thread(target=open_view)
        worker.start()
        try:
            _wait_pending(path, 1)
            assert opened == [] and live_calls == []
            assert try_fast(path, eligible=True).reason == REASON_LEGACY_PENDING
            occupying.lease.finish(source_released=True)
            assert ready.wait(5) and not errors
            assert opened and str(opened[0]).endswith("?mode=ro") and live_calls
            assert GATE.snapshot(path).legacy_active == 1
            assert try_fast(path, eligible=True).reason == REASON_LEGACY_ENROLLED
        finally:
            occupying.lease.finish(source_released=True)
            release.set()
            worker.join(5)
        assert not worker.is_alive() and not errors
        _assert_idle(path)

    def test_new_fault_blocks_before_live_check(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "new-fault.kohakutr")
        admitted = try_fast(path, eligible=True)
        assert admitted.lease is not None
        admitted.lease.finish(source_released=False)
        live_calls, opened = [], []

        def spy_live(target):
            live_calls.append(target)
            raise AssertionError("live check must not run after NEW fault")

        def boom_connect(*args, **kwargs):
            opened.append(args)
            raise AssertionError("must not open SQL after NEW fault")

        monkeypatch.setattr(readonly_view, "live_store_for", spy_live)
        monkeypatch.setattr(readonly_view.sqlite3, "connect", boom_connect)
        with pytest.raises(ResourceReleaseError):
            SessionReadView(path)
        assert live_calls == [] and opened == []
        with pytest.raises(ResourceReleaseError):
            try_fast(path, eligible=True)

    def test_live_view_holds_reservation_after_owner_close(self, tmp_path, monkeypatch):
        path = tmp_path / "owner-close.kohakutr"
        store = SessionStore(path)
        _populate(store)
        monkeypatch.setattr(readonly_view, "sqlite3", _ForbiddenSqlite())
        reader = SessionReadView(path)
        try:
            store.close()
            assert live_store_for(path) is None
            assert try_fast(path, eligible=True).reason == REASON_LEGACY_ENROLLED
        finally:
            reader.close()
        _assert_idle(path)

    def test_live_close_does_not_close_borrowed_store(self, tmp_path, monkeypatch):
        path = tmp_path / "borrowed.kohakutr"
        store = SessionStore(path)
        try:
            _populate(store)
            monkeypatch.setattr(readonly_view, "sqlite3", _ForbiddenSqlite())
            SessionReadView(path).close()
            assert store.get_events("alice")[0]["content"] == "first"
            assert GATE.snapshot(path).legacy_active == 1
            assert live_store_for(path) is store
        finally:
            store.close()
        _assert_idle(path)


class TestReadViewCleanup:
    def test_connect_failure_is_conservatively_uncertain(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "connect-fail.kohakutr")
        monkeypatch.setattr(
            readonly_view.sqlite3,
            "connect",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("connect failed without a handle")
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="connect failed"):
            SessionReadView(path)
        assert try_fast(path, eligible=True).reason == UNCERTAIN

    def test_partial_constructor_cleans_assigned_db(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "partial.kohakutr")
        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        class Recording(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        def connect(target, *args, **kwargs):
            kwargs["factory"] = Recording
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(readonly_view.sqlite3, "connect", connect)
        monkeypatch.setattr(
            readonly_view,
            "KVault",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("decoder failed")),
        )
        with pytest.raises(RuntimeError, match="decoder failed") as caught:
            SessionReadView(path)
        assert caught.value.__cause__ is None and opened
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            opened[0].execute("SELECT 1")
        _assert_idle(path)

    def test_constructor_preserves_primary_when_db_close_fails(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "ctor-close.kohakutr")
        _FailingCloseConnection.instances = []
        _connect_with_factory(monkeypatch, _FailingCloseConnection)
        monkeypatch.setattr(
            readonly_view,
            "KVault",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("decoder failed")),
        )
        try:
            with pytest.raises(RuntimeError, match="decoder failed") as caught:
                SessionReadView(path)
            assert "db close failed" in str(caught.value.__cause__)
            assert try_fast(path, eligible=True).reason == UNCERTAIN
        finally:
            _force_close_failing()

    def test_db_close_failure_disables_new_and_still_closes_decoder(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "db-close.kohakutr")
        _FailingCloseConnection.instances = []
        decoder_closed = []
        real_kvault = readonly_view.KVault
        _connect_with_factory(monkeypatch, _FailingCloseConnection)

        class RecordingVault(real_kvault):
            def close(self):
                decoder_closed.append(True)
                return super().close()

        monkeypatch.setattr(readonly_view, "KVault", RecordingVault)
        reader = SessionReadView(path)
        try:
            with pytest.raises(RuntimeError, match="db close failed"):
                reader.close()
            assert decoder_closed == [True]
            reader.close()
            assert try_fast(path, eligible=True).reason == UNCERTAIN
        finally:
            _force_close_failing()

    def test_decoder_close_failure_after_db_close_does_not_quarantine_source(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "decoder-close.kohakutr")
        real_kvault = readonly_view.KVault
        closed_db = []
        real_connect = sqlite3.connect

        class Recording(sqlite3.Connection):
            def close(self):
                closed_db.append(True)
                return super().close()

        def connect(target, *args, **kwargs):
            kwargs["factory"] = Recording
            return real_connect(target, *args, **kwargs)

        class BoomVault(real_kvault):
            def close(self):
                raise RuntimeError("decoder close failed")

        monkeypatch.setattr(readonly_view.sqlite3, "connect", connect)
        monkeypatch.setattr(readonly_view, "KVault", BoomVault)
        reader = SessionReadView(path)
        with pytest.raises(RuntimeError, match="decoder close failed"):
            reader.close()
        assert closed_db == [True]
        reader.close()
        _assert_idle(path)

    def test_exit_does_not_swallow_body_error(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "exit-body.kohakutr")
        _FailingCloseConnection.instances = []
        _connect_with_factory(monkeypatch, _FailingCloseConnection)
        reader = SessionReadView(path)
        try:
            with pytest.raises(ValueError, match="body failed") as caught:
                with reader:
                    raise ValueError("body failed")
            assert "db close failed" in str(caught.value.__cause__)
        finally:
            _force_close_failing()
        assert try_fast(path, eligible=True).reason == UNCERTAIN

    def test_exit_propagates_close_error_when_body_succeeds(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "exit-close.kohakutr")
        _FailingCloseConnection.instances = []
        _connect_with_factory(monkeypatch, _FailingCloseConnection)
        try:
            with pytest.raises(RuntimeError, match="db close failed"):
                with SessionReadView(path):
                    pass
        finally:
            _force_close_failing()

    def test_canceled_worker_keeps_lease_until_close(self, tmp_path):
        path = _closed_session(tmp_path / "cancel.kohakutr")
        hold, entered, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def hold_view():
            view = SessionReadView(path)
            entered.set()
            try:
                hold.wait(5)
            finally:
                view.close()
                finished.set()

        async def run():
            task = asyncio.create_task(asyncio.to_thread(hold_view))
            assert await asyncio.to_thread(entered.wait, 3)
            assert GATE.snapshot(path).legacy_active == 1
            task.cancel()
            await asyncio.sleep(0.05)
            assert not finished.is_set() and GATE.snapshot(path).legacy_active == 1
            hold.set()
            await asyncio.gather(task, return_exceptions=True)
            assert finished.wait(2)
            _assert_idle(path)

        asyncio.run(run())


class TestReadViewCursors:
    def test_close_closes_active_cursor_before_db(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "cursor.kohakutr")
        _TrackingConnection.events = []
        _connect_with_factory(monkeypatch, _TrackingConnection)
        reader = SessionReadView(path)
        gen = reader.items("events")
        assert next(gen)[0].endswith(":e000000")
        reader.close()
        kinds = [
            item[0] if isinstance(item, tuple) else item
            for item in _TrackingConnection.events
        ]
        assert kinds.index("cursor.close") < kinds.index("db.close")
        close_events = [
            item
            for item in _TrackingConnection.events
            if item in ("cursor.close", "db.close")
        ]
        assert close_events[-2:] == ["cursor.close", "db.close"]
        _assert_idle(path)

    def test_completed_cursors_close_without_closing_active_items(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "complete.kohakutr")
        _TrackingConnection.events = []
        _connect_with_factory(monkeypatch, _TrackingConnection)
        reader = SessionReadView(path)
        assert _TrackingConnection.events.count("cursor.close") >= 2
        gen = reader.items("events")
        next(gen)
        before_get = _TrackingConnection.events.count("cursor.close")
        assert reader.get("meta", "session_id") == "sid"
        assert _TrackingConnection.events.count("cursor.close") == before_get + 1
        assert next(gen)[0] == "alice:e000000"
        reader.close()
        _assert_idle(path)

    def test_failed_cursor_close_stays_uncertain_after_later_success(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "retain.kohakutr")
        _connect_with_factory(monkeypatch, _GetCursorCloseFails)
        reader = SessionReadView(path)
        gen = reader.items("events")
        next(gen)
        assert reader.get("meta", "nested")["items"][-1] == "汉字"
        assert next(gen)[0] == "alice:e000000"
        reader.close()
        assert try_fast(path, eligible=True).reason == UNCERTAIN

    def test_tracking_failure_after_execute_keeps_source_uncertain(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "track-fail.kohakutr")
        _connect_with_factory(monkeypatch, _TrackingConnection)
        reader = SessionReadView(path)

        class BoomList(list):
            def append(self, item):
                raise RuntimeError("cannot track cursor")

        reader._cursors = BoomList(reader._cursors)
        with pytest.raises(RuntimeError, match="cannot track cursor"):
            reader.get("meta", "nested")
        reader.close()
        assert try_fast(path, eligible=True).reason == UNCERTAIN

    def test_stale_generator_does_not_open_cursor_after_lease_finish(
        self, tmp_path, monkeypatch
    ):
        path = _closed_session(tmp_path / "stale.kohakutr")
        _TrackingConnection.events = []
        _connect_with_factory(monkeypatch, _TrackingConnection)
        reader = SessionReadView(path)
        gen = reader.items("events")
        reader.close()
        baseline = sum(
            1 for item in _TrackingConnection.events if isinstance(item, tuple)
        )
        with pytest.raises((StopIteration, sqlite3.Error)):
            next(gen)
        later = sum(1 for item in _TrackingConnection.events if isinstance(item, tuple))
        assert later == baseline
        _assert_idle(path)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            reader.get("meta", "nested")
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            list(reader.items("events"))

    def test_python_sqlite_close_leaves_open_cursor_unusable(self, tmp_path):
        path = tmp_path / "native.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(k TEXT)")
        conn.execute("INSERT INTO t VALUES ('a')")
        conn.commit()
        cursor = conn.execute("SELECT k FROM t")
        conn.close()
        conn.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            next(iter(cursor))
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            cursor.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


class TestReadViewLimitations:
    def test_memory_and_view_still_nest_as_legacy(self, tmp_path):
        path = _closed_session(tmp_path / "memory.kohakutr")
        memory = SessionMemory(str(path))
        try:
            reader = SessionReadView(path)
            try:
                assert GATE.snapshot(path).legacy_active == 2
                assert try_fast(path, eligible=True).reason == REASON_LEGACY_ENROLLED
            finally:
                reader.close()
            assert GATE.snapshot(path).legacy_active == 1
        finally:
            memory.close()
        _assert_idle(path)

    def test_registry_lookup_is_not_atomic_with_sql_open(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "race.kohakutr")
        order: list[str] = []
        real_live, real_connect = readonly_view.live_store_for, sqlite3.connect

        def spy_live(target):
            order.append("live")
            return real_live(target)

        def spy_connect(target, *a, **k):
            order.append("sql")
            return real_connect(target, *a, **k)

        monkeypatch.setattr(readonly_view, "live_store_for", spy_live)
        monkeypatch.setattr(readonly_view.sqlite3, "connect", spy_connect)
        with SessionReadView(path):
            assert order == ["live", "sql"]
        _assert_idle(path)

    def test_cross_thread_close_failure_stays_uncertain(self, tmp_path, monkeypatch):
        path = _closed_session(tmp_path / "cross-thread.kohakutr")
        _FailingCloseConnection.instances = []
        _connect_with_factory(monkeypatch, _FailingCloseConnection)
        reader = SessionReadView(path)
        errors: list[BaseException] = []

        def closer():
            try:
                reader.close()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=closer)
        try:
            worker.start()
            worker.join(5)
            assert errors
            assert try_fast(path, eligible=True).reason == UNCERTAIN
        finally:
            _force_close_failing()
