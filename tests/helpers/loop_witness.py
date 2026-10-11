"""Witness that an operation runs off the event loop, independent of machine speed.

Call :func:`loop_ran_meanwhile` from inside the operation under test. It
schedules a callback on the loop and waits for it: off-loop, the loop runs it
at once; on the loop thread, the callback cannot run until the operation
returns, so the wait times out and the witness reports ``False``.
"""

import asyncio
import threading

WITNESS_TIMEOUT = 10.0


def loop_ran_meanwhile(
    loop: asyncio.AbstractEventLoop, timeout: float = WITNESS_TIMEOUT
) -> bool:
    """True when ``loop`` ran a callback while the calling operation was in flight."""
    ran = threading.Event()
    loop.call_soon_threadsafe(ran.set)
    return ran.wait(timeout)


class LoopWitness:
    """Collects one :func:`loop_ran_meanwhile` result per observed call."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.results: list[bool] = []

    def observe(self) -> None:
        """Record whether the loop stayed live during the current call."""
        self.results.append(loop_ran_meanwhile(self.loop))

    @property
    def never_blocked(self) -> bool:
        """At least one call was observed and the loop stayed live in each."""
        return bool(self.results) and all(self.results)
