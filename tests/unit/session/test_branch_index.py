"""Branch maxima follow native event writes and rebuild after store copies."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kohakuterrarium.session.store import SessionStore


class ObservedTable:
    def __init__(self, table, on_read):
        self.table = table
        self.on_read = on_read

    def __getattr__(self, name):
        return getattr(self.table, name)

    def __getitem__(self, key):
        self.on_read()
        return self.table[key]

    def __setitem__(self, key, value):
        self.table[key] = value


def test_maxima_survive_append_mirror_fork_and_reopen(tmp_path):
    path = tmp_path / "parent.kohakutr"
    store = SessionStore(path)
    original_table = store.events
    reads = []
    try:
        store.append_event(
            "a", "user_message", {"content": "original"}, turn_index=1, branch_id=2
        )
        store.append_event(
            "b", "user_message", {"content": "other"}, turn_index=1, branch_id=9
        )
        store.events = ObservedTable(store.events, lambda: reads.append(1))
        assert store.max_branch_id("a", 1) == 2
        assert reads
        reads.clear()
        for turn in (1, 2, 3):
            assert store.max_branch_id("a", turn) == (2 if turn == 1 else 0)
        assert reads == []
        _, cut = store.append_event(
            "a",
            "user_message",
            {"content": "mirrored", "event_id": 500},
            turn_index=2,
            branch_id=7,
        )
        replaced_key, _ = store.append_event("a", "text", {}, turn_index=1, branch_id=4)
        assert store.max_branch_id("a", 1) == 4
        assert store.max_branch_id("a", 2) == 7
        assert reads == []
        child = store.fork(str(tmp_path / "child.kohakutr"), at_event_id=cut)
        try:
            assert child.max_branch_id("a", 1) == 2
            assert child.max_branch_id("a", 2) == 7
            assert child.max_branch_id("b", 1) == 9
        finally:
            child.close()
        store.events[replaced_key] = {
            "type": "user_message",
            "event_id": 600,
            "turn_index": 3,
            "branch_id": 8,
        }
        store._restore_counters()
        assert store.max_branch_id("a", 3) == 8
    finally:
        store.events = original_table
        store.close()
    reopened = SessionStore.open_readonly(path)
    try:
        assert reopened.max_branch_id("a", 1) == 2
        assert reopened.max_branch_id("a", 3) == 8
        assert reopened.max_branch_id("missing", 1) == 0
    finally:
        reopened.close()


def test_failed_cold_read_does_not_cache_missing_branches(tmp_path):
    store = SessionStore(tmp_path / "s.kohakutr")
    original_table = store.events
    try:
        store.append_event("a", "text", {}, turn_index=1, branch_id=5)
        failed = False

        def fail_once():
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("transient read failure")

        store.events = ObservedTable(store.events, fail_once)
        with pytest.raises(OSError, match="transient"):
            store.max_branch_id("a", 1)
        assert store.max_branch_id("a", 1) == 5
    finally:
        store.events = original_table
        store.close()


def test_append_during_cold_scan_returns_latest_maximum(tmp_path):
    store = SessionStore(tmp_path / "s.kohakutr")
    original_table = store.events
    entered, release = threading.Event(), threading.Event()
    try:
        store.append_event("a", "text", {}, turn_index=1, branch_id=1)
        store._flush_events_cache()

        def pause_read():
            entered.set()
            assert release.wait(5)

        store.events = ObservedTable(store.events, pause_read)
        with ThreadPoolExecutor(max_workers=1) as executor:
            reading = executor.submit(store.max_branch_id, "a", 1)
            try:
                assert entered.wait(3)
                store.append_event("a", "text", {}, turn_index=1, branch_id=6)
            finally:
                release.set()
            assert reading.result(timeout=3) == 6
        assert store.max_branch_id("a", 1) == 6
    finally:
        release.set()
        store.events = original_table
        store.close()
