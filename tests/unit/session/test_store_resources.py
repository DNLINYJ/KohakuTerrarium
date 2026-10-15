"""SessionStore occupancy enrollment around native open and close."""

from __future__ import annotations

import threading
import time

import pytest

import kohakuterrarium.session.store as store_mod
import kohakuterrarium.session.store_lock as store_lock_mod
import kohakuterrarium.session.store_resources as store_resources_mod
from kohakuterrarium.errors import SessionLockedError
from kohakuterrarium.session.open_registry import live_store_for
from kohakuterrarium.session.resource_gate import (
    GATE,
    REASON_LEGACY_ENROLLED,
    REASON_LEGACY_PENDING,
    REASON_LEGACY_RELEASE_UNCERTAIN,
    REASON_OK,
    try_fast,
)
from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.session.store_lock import close_tables, discard_partial_open


def _wait_pending(path, count: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if GATE.snapshot(path).legacy_pending == count:
            return
        time.sleep(0.01)
    raise AssertionError(f"pending occupancy did not reach {count}")


def _drop_native(obj, attr: str) -> None:
    if hasattr(obj, attr):
        delattr(obj, attr)


def _manual_cleanup_handles(handles, fts=None) -> None:
    """Best-effort native teardown after a test observed a leaked constructor."""
    for table in handles:
        try:
            disable = getattr(table, "disable_cache", None)
            if callable(disable):
                disable()
        except Exception:
            pass
        try:
            close = getattr(table, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        _drop_native(table, "_inner")
    if fts is not None:
        try:
            fts.close()
        except Exception:
            pass
        _drop_native(fts, "_vault")


class TestStoreEnrollment:
    def test_open_enrolls_legacy_and_close_releases(self, tmp_path):
        path = tmp_path / "enroll.kohakutr"
        store = SessionStore(path)
        try:
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 1
            assert snap.legacy_pending == 0
            denied = try_fast(path, eligible=True)
            assert denied.lease is None
            assert denied.reason == REASON_LEGACY_ENROLLED
        finally:
            store.close()
        snap = GATE.snapshot(path)
        assert snap.registered is False
        admitted = try_fast(path, eligible=True)
        assert admitted.reason == REASON_OK
        assert admitted.lease is not None
        admitted.lease.finish(source_released=True)

    def test_nested_legacy_opens_remain_allowed(self, tmp_path):
        path = tmp_path / "nested.kohakutr"
        first = SessionStore(path)
        second = SessionStore(path)
        try:
            assert GATE.snapshot(path).legacy_active == 2
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_ENROLLED
        finally:
            first.close()
            assert GATE.snapshot(path).legacy_active == 1
            second.close()
        assert GATE.snapshot(path).registered is False

    def test_register_before_native_open_and_writer_lock(self, tmp_path, monkeypatch):
        path = tmp_path / "order.kohakutr"
        seen = {}
        real_open = SessionStore._open_tables
        real_lock = store_resources_mod.acquire_writer_lock

        def spy_open(self):
            seen["legacy_at_open"] = GATE.snapshot(path).legacy_active
            seen["live_at_open"] = live_store_for(path) is self
            real_open(self)

        def spy_lock(lock_path):
            seen["legacy_at_lock"] = GATE.snapshot(lock_path).legacy_active
            seen["live_at_lock"] = live_store_for(lock_path) is not None
            return real_lock(lock_path)

        monkeypatch.setattr(SessionStore, "_open_tables", spy_open)
        monkeypatch.setattr(store_resources_mod, "acquire_writer_lock", spy_lock)
        store = SessionStore(path, writer_lock=True)
        try:
            assert seen["legacy_at_lock"] == 1
            assert seen["live_at_lock"] is True
            assert seen["legacy_at_open"] == 1
            assert seen["live_at_open"] is True
        finally:
            store.close()

    def test_failed_writer_lock_releases_without_uncertainty(self, tmp_path):
        path = tmp_path / "lockfail.kohakutr"
        holder = SessionStore(path, writer_lock=True)
        try:
            with pytest.raises(SessionLockedError):
                SessionStore(path, writer_lock=True)
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 1
            assert snap.legacy_uncertain is False
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_ENROLLED
        finally:
            holder.close()
        assert GATE.snapshot(path).registered is False
        admitted = try_fast(path, eligible=True)
        assert admitted.reason == REASON_OK
        admitted.lease.finish(source_released=True)

    def test_partial_open_keeps_uncertainty_if_native_may_own_handle(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "partial.kohakutr"

        def boom(self):
            raise RuntimeError("table open failed")

        with monkeypatch.context() as patched:
            patched.setattr(SessionStore, "_open_tables", boom)
            with pytest.raises(RuntimeError):
                SessionStore(path)
        snap = GATE.snapshot(path)
        assert snap.legacy_active == 0
        assert snap.legacy_uncertain is True
        denied = try_fast(path, eligible=True)
        assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN
        retry = SessionStore(path)
        try:
            assert GATE.snapshot(path).legacy_active == 1
            still = try_fast(path, eligible=True)
            assert still.reason == REASON_LEGACY_RELEASE_UNCERTAIN
        finally:
            retry.close()
        still = try_fast(path, eligible=True)
        assert still.reason == REASON_LEGACY_RELEASE_UNCERTAIN

    def test_partial_open_after_assigned_tables_stays_uncertain(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "assigned.kohakutr"
        opened = []
        real_kvault = store_mod.KVault

        class RecordingKVault(real_kvault):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        def boom(*args, **kwargs):
            raise RuntimeError("fts open failed")

        monkeypatch.setattr(store_mod, "KVault", RecordingKVault)
        monkeypatch.setattr(store_mod, "TextVault", boom)
        with pytest.raises(RuntimeError):
            SessionStore(path)
        assert opened
        assert all(not hasattr(table, "_inner") for table in opened)
        snap = GATE.snapshot(path)
        assert snap.legacy_uncertain is True
        denied = try_fast(path, eligible=True)
        assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN

    def test_companion_failure_cannot_enable_new(self, tmp_path):
        path = tmp_path / "companion.kohakutr"
        store = SessionStore(path)

        def boom():
            raise RuntimeError("closer boom")

        store.register_companion_closer(boom)
        store.close()
        snap = GATE.snapshot(path)
        assert snap.legacy_uncertain is True
        denied = try_fast(path, eligible=True)
        assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN
        retry = SessionStore(path)
        try:
            assert GATE.snapshot(path).legacy_active == 1
        finally:
            retry.close()
        still = try_fast(path, eligible=True)
        assert still.reason == REASON_LEGACY_RELEASE_UNCERTAIN

    def test_self_close_rejection_does_not_release_lease(self, tmp_path):
        path = tmp_path / "self-close.kohakutr"
        store = SessionStore(path)
        try:
            with pytest.raises(RuntimeError, match="affinity worker"):
                store.submit(store.close).result(timeout=5)
            assert store._closed is False
            assert GATE.snapshot(path).legacy_active == 1
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_ENROLLED
            store.submit(store.state.put, "still-open", "value").result(timeout=5)
        finally:
            store.close()
        assert GATE.snapshot(path).registered is False

    def test_idempotent_close_does_not_clear_uncertainty(self, tmp_path):
        path = tmp_path / "idempotent.kohakutr"
        store = SessionStore(path)

        def boom():
            raise RuntimeError("closer boom")

        store.register_companion_closer(boom)
        store.close()
        assert GATE.snapshot(path).legacy_uncertain is True
        store.close()
        snap = GATE.snapshot(path)
        assert snap.legacy_uncertain is True
        denied = try_fast(path, eligible=True)
        assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN

    def test_sqlite_reopen_after_release(self, tmp_path):
        path = tmp_path / "sqlite.kohakutr"
        store = SessionStore(path)
        store.append_event("a", "text", {"content": "one"})
        store.close()
        reopened = SessionStore(path)
        try:
            events = reopened.get_events("a")
            assert events[0]["content"] == "one"
        finally:
            reopened.close()
        admitted = try_fast(path, eligible=True)
        assert admitted.reason == REASON_OK
        admitted.lease.finish(source_released=True)

    def test_pending_enrollment_blocks_new_before_native_open(self, tmp_path):
        path = tmp_path / "pending.kohakutr"
        occupying = try_fast(path, eligible=True)
        assert occupying.lease is not None
        stores: list[SessionStore] = []
        errors: list[BaseException] = []

        def open_store():
            try:
                stores.append(SessionStore(path))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=open_store)
        worker.start()
        try:
            _wait_pending(path, 1)
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_PENDING
            occupying.lease.finish(source_released=True)
            worker.join(5)
            assert not worker.is_alive()
            assert not errors
            assert GATE.snapshot(path).legacy_active == 1
            denied_open = try_fast(path, eligible=True)
            assert denied_open.reason == REASON_LEGACY_ENROLLED
        finally:
            occupying.lease.finish(source_released=True)
            worker.join(5)
            for store in stores:
                store.close()


class TestCloseTablesCertainty:
    def test_successful_close_returns_true(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def close(self):
                return None

        class Fts:
            def __init__(self):
                self._vault = object()

        table = Table()
        fts = Fts()
        assert close_tables((table,), fts, None) is True
        assert not hasattr(table, "_inner")
        assert not hasattr(fts, "_vault")

    def test_companion_failure_returns_false(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def close(self):
                return None

        class Fts:
            def __init__(self):
                self._vault = object()

        def boom():
            raise RuntimeError("closer")

        table = Table()
        assert close_tables((table,), Fts(), None, (boom,)) is False
        assert not hasattr(table, "_inner")

    def test_table_close_failure_propagates(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def close(self):
                raise RuntimeError("close failed")

        with pytest.raises(RuntimeError):
            close_tables((Table(),), None, None)


class TestDiscardPartialOpenCertainty:
    def test_missing_table_is_uncertain(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def disable_cache(self):
                return None

            def close(self):
                return None

        class Fts:
            def __init__(self):
                self._vault = object()

            def close(self):
                return None

        table = Table()
        fts = Fts()
        assert (
            discard_partial_open(
                [("events", table), ("meta", None)],
                fts,
                "s.kohakutr",
            )
            is False
        )
        assert not hasattr(table, "_inner")
        assert not hasattr(fts, "_vault")

    def test_all_handles_dropped_returns_true(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def disable_cache(self):
                return None

            def close(self):
                return None

        class Fts:
            def __init__(self):
                self._vault = object()

            def close(self):
                return None

        tables = [(name, Table()) for name in store_lock_mod.TABLE_ATTRS]
        fts = Fts()
        assert discard_partial_open(tables, fts, "s.kohakutr") is True
        assert all(not hasattr(table, "_inner") for _, table in tables)
        assert not hasattr(fts, "_vault")

    def test_missing_native_attr_is_uncertain(self):
        class Table:
            def disable_cache(self):
                return None

            def close(self):
                return None

        class Fts:
            def __init__(self):
                self._vault = object()

            def close(self):
                return None

        assert (
            discard_partial_open(
                [(name, Table()) for name in store_lock_mod.TABLE_ATTRS],
                Fts(),
                "s.kohakutr",
            )
            is False
        )

    def test_missing_fts_is_uncertain(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def disable_cache(self):
                return None

            def close(self):
                return None

        tables = [(name, Table()) for name in store_lock_mod.TABLE_ATTRS]
        assert discard_partial_open(tables, None, "s.kohakutr") is False

    def test_swallowed_cleanup_error_does_not_claim_release(self):
        class Table:
            def __init__(self):
                self._inner = object()

            def disable_cache(self):
                raise RuntimeError("cache disable failed")

            def close(self):
                raise RuntimeError("close failed")

        class Fts:
            def __init__(self):
                self._vault = object()

            def close(self):
                return None

        tables = [(name, Table()) for name in store_lock_mod.TABLE_ATTRS]
        fts = Fts()
        assert discard_partial_open(tables, fts, "s.kohakutr") is False
        assert all(not hasattr(table, "_inner") for _, table in tables)
        assert not hasattr(fts, "_vault")


class TestDiscardFailedOpenBaseException:
    def test_keyboard_interrupt_during_discard_still_finishes_lease(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "discard-interrupt.kohakutr"
        opened = []
        real_kvault = store_mod.KVault

        class RecordingKVault(real_kvault):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        def boom(*args, **kwargs):
            raise RuntimeError("fts open failed")

        real_discard = store_lock_mod.discard_partial_open

        def interrupt_discard(*args, **kwargs):
            real_discard(*args, **kwargs)
            raise KeyboardInterrupt("discard interrupted")

        monkeypatch.setattr(store_mod, "KVault", RecordingKVault)
        monkeypatch.setattr(store_mod, "TextVault", boom)
        monkeypatch.setattr(store_lock_mod, "discard_partial_open", interrupt_discard)
        monkeypatch.setattr(
            store_resources_mod, "discard_partial_open", interrupt_discard
        )
        with pytest.raises(KeyboardInterrupt):
            SessionStore(path, writer_lock=True)
        try:
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 0
            assert snap.legacy_uncertain is True
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN
            assert live_store_for(path) is None
            monkeypatch.undo()
            retry = SessionStore(path, writer_lock=True)
            retry.close()
        finally:
            _manual_cleanup_handles(opened)

    def test_constructor_cleanup_interrupted_with_assigned_handles(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "constructor-interrupt.kohakutr"
        opened = []
        real_kvault = store_mod.KVault

        class RecordingKVault(real_kvault):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        def boom(*args, **kwargs):
            raise KeyboardInterrupt("constructor interrupted")

        monkeypatch.setattr(store_mod, "KVault", RecordingKVault)
        monkeypatch.setattr(store_mod, "TextVault", boom)
        with pytest.raises(KeyboardInterrupt):
            SessionStore(path, writer_lock=True)
        try:
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 0
            assert live_store_for(path) is None
            monkeypatch.undo()
            retry = SessionStore(path, writer_lock=True)
            retry.close()
        finally:
            _manual_cleanup_handles(opened)


class TestCloseInterruptAndSelfClose:
    def test_interrupt_during_drain_does_not_dispose_undrained_workers(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "interrupt-drain.kohakutr"
        store = SessionStore(path)
        started = threading.Event()
        release = threading.Event()
        worker_done = threading.Event()
        disposed = []

        def blocked():
            started.set()
            try:
                assert release.wait(5)
            finally:
                worker_done.set()
            return "ran"

        future = store.submit(blocked)
        assert started.wait(5)
        original_shutdown = store._shutdown_affinity

        def interrupt_shutdown():
            raise KeyboardInterrupt("drain interrupted")

        monkeypatch.setattr(store, "_shutdown_affinity", interrupt_shutdown)
        original_close_enrolled = store._close_enrolled_resources

        def tracking_close():
            disposed.append(True)
            original_close_enrolled()

        monkeypatch.setattr(store, "_close_enrolled_resources", tracking_close)
        try:
            with pytest.raises(KeyboardInterrupt):
                store.close()
            assert disposed == []
            assert GATE.snapshot(path).legacy_active == 0
            assert GATE.snapshot(path).legacy_uncertain
            assert store._closed is True
            assert not future.done()
        finally:
            release.set()
            worker_done.wait(5)
            monkeypatch.undo()
            original_shutdown()
            store._close_enrolled_resources()
            if not future.done():
                future.result(timeout=5)

    def test_interrupt_during_metadata_still_attempts_cleanup(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "interrupt-meta.kohakutr"
        store = SessionStore(path)
        cleaned = []
        original_close_enrolled = store._close_enrolled_resources

        def tracking_close():
            cleaned.append(True)
            original_close_enrolled()

        def boom_status(status):
            raise KeyboardInterrupt("status interrupted")

        monkeypatch.setattr(store, "_close_enrolled_resources", tracking_close)
        monkeypatch.setattr(store, "update_status", boom_status)
        try:
            with pytest.raises(KeyboardInterrupt):
                store.close()
            assert cleaned == [True]
            snap = GATE.snapshot(path)
            assert snap.legacy_active == 0
            assert store._resource_lease is None
        finally:
            if not cleaned:
                original_close_enrolled()

    def test_table_close_failure_retains_old_propagate_behavior(self, tmp_path):
        path = tmp_path / "table-close-propagate.kohakutr"
        store = SessionStore(path)
        original_close = store.events.close

        def boom():
            raise RuntimeError("table close failed")

        store.events.close = boom
        try:
            with pytest.raises(RuntimeError, match="table close failed"):
                store.close()
            snap = GATE.snapshot(path)
            assert snap.legacy_uncertain is True
            denied = try_fast(path, eligible=True)
            assert denied.reason == REASON_LEGACY_RELEASE_UNCERTAIN
        finally:
            store.events.close = original_close
            store._close_enrolled_resources()
