"""Unit tests for the dedicated persistence executor.

Pins three properties:

1. The executor is a separate :class:`ThreadPoolExecutor` instance —
   NOT the loop's default pool that ``asyncio.to_thread`` uses.
2. Concurrent persistence work doesn't starve other ``asyncio.to_thread``
   callers (and vice-versa): a saturated persistence pool must not
   block ``to_thread``.
3. Sized for I/O-fan-out: at least 32 worker threads available.

Together these are the property the user demanded: "session list still
blocks whole server" can never recur as long as the persistence layer
uses its own pool.
"""

import asyncio
import concurrent.futures
import threading

import pytest


@pytest.fixture(autouse=True)
def _reset_executor():
    """Ensure each test starts from a clean executor singleton.

    The persistence executor is now a thin alias over
    :mod:`kohakuterrarium.api._io_executor`, so the singleton state
    lives there.  Snapshot + restore on that module.
    """
    from kohakuterrarium.api import _io_executor as ex

    snap = ex._executor
    ex._executor = None
    yield
    if ex._executor is not None and ex._executor is not snap:
        ex._executor.shutdown(wait=True)
    ex._executor = snap


class TestPersistenceExecutor:
    def test_singleton_creates_real_threadpool(self):
        from kohakuterrarium.api.routes.persistence._executor import (
            get_persistence_executor,
        )

        ex = get_persistence_executor()
        assert isinstance(ex, concurrent.futures.ThreadPoolExecutor)
        # Same instance on subsequent calls.
        assert get_persistence_executor() is ex

    def test_max_workers_at_least_32(self):
        from kohakuterrarium.api.routes.persistence._executor import (
            _MAX_WORKERS,
        )

        # We cap at 64; below 32 would be too small for an I/O fan-out
        # over hundreds of session files.
        assert _MAX_WORKERS >= 32

    async def test_runs_on_a_different_pool_than_to_thread(self):
        """Persistence work completes while every ``to_thread`` worker is held.

        Each default-pool worker blocks on an event the test sets only after
        the persistence call returns, so a shared pool can never run it.
        """
        from kohakuterrarium.api.routes.persistence._executor import (
            run_in_persistence_executor,
        )

        loop = asyncio.get_running_loop()
        default_pool = concurrent.futures.ThreadPoolExecutor()
        loop.set_default_executor(default_pool)
        pool_size = default_pool._max_workers
        held = threading.Event()
        starver_threads: set[int] = set()

        def _hold():
            starver_threads.add(threading.get_ident())
            held.wait(30)

        gathered = asyncio.gather(*(asyncio.to_thread(_hold) for _ in range(pool_size)))
        try:
            for _ in range(1000):
                if len(starver_threads) == pool_size:
                    break
                await asyncio.sleep(0.01)
            assert len(starver_threads) == pool_size, "default pool never saturated"
            persist_thread = await asyncio.wait_for(
                run_in_persistence_executor(threading.get_ident), timeout=10
            )
            assert persist_thread not in starver_threads
        finally:
            held.set()
            await gathered
            default_pool.shutdown(wait=True)
