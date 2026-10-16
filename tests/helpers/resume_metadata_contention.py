"""Real HTTP resume under an in-process candidate SQL-close barrier."""

import asyncio
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from kohakuterrarium.session import metadata_candidate as candidate
from kohakuterrarium.session import resume_async
from kohakuterrarium.session import store as store_module
from kohakuterrarium.session.resource_gate import GATE
from kohakuterrarium.terrarium import resume, resume_manifest


def resume_with_metadata_contention(client, path, url, monkeypatch):
    """Resume through HTTP while loop progress releases a real competing source read."""
    entered, release, closed = (threading.Event() for _ in range(3))
    finished, timed_out = threading.Event(), threading.Event()
    real_vault = store_module.KVault
    readers, errors, outcomes, ticks = [], [], [], []

    class Connection(sqlite3.Connection):
        def close(self):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("candidate close not released")
            super().close()
            closed.set()

    def connect(*args, **kwargs):
        return sqlite3.connect(*args, factory=Connection, **kwargs)

    def read():
        try:
            outcomes.append(candidate.read_metadata_candidate(path, eligible=True))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def wrap(opener):
        def open_store(target, **kwargs):
            if Path(target).resolve() == path.resolve():
                assert not readers, "unexpected second owned open"
                reader = threading.Thread(target=read)
                readers.append(reader)
                reader.start()
                assert entered.wait(10)
                timer.start()
            return opener(target, **kwargs)

        return open_store

    def guarded_vault(target, *args, **kwargs):
        if readers and Path(target).resolve() == path.resolve():
            assert closed.is_set(), "native source open before SQL release"
        return real_vault(target, *args, **kwargs)

    async def unblock_from_loop():
        while not entered.is_set():
            await asyncio.sleep(0.005)
        while not GATE.snapshot(path).legacy_pending:
            if release.is_set():
                return
            await asyncio.sleep(0.005)
        for _ in range(5):
            await asyncio.sleep(0.005)
            ticks.append(True)
        release.set()

    def watchdog():
        timed_out.set()
        release.set()

    timer = threading.Timer(10, watchdog)
    with monkeypatch.context() as scoped:
        scoped.setattr(candidate, "sqlite3", SimpleNamespace(connect=connect))
        scoped.setattr(store_module, "KVault", guarded_vault)
        for module in (resume, resume_manifest, resume_async):
            scoped.setattr(
                module,
                "_open_store_with_migration",
                wrap(module._open_store_with_migration),
            )
        progress = client.portal.start_task_soon(unblock_from_loop)
        try:
            response = client.post(url)
        finally:
            release.set()
            timer.cancel()
            if timer.ident is not None:
                timer.join()
            progress.cancel()
            for reader in readers:
                reader.join(10)
    assert not timed_out.is_set(), "actual HTTP resume blocked event-loop release"
    assert readers and finished.is_set() and closed.is_set()
    assert all(not reader.is_alive() for reader in readers)
    assert not errors and [result.reason for result in outcomes] == ["ok"]
    assert len(ticks) == 5
    assert outcomes[0].meta["session_id"]
    return response
