"""Worker-owned occupancy for exact-path Laboratory file mutations."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from kohakuterrarium.session.resource_gate import acquire_legacy

T = TypeVar("T")


async def run_mutation_worker(
    fn: Callable[..., T],
    /,
    *args: object,
    on_success: Callable[[], None] | None = None,
) -> T:
    """Run a sync mutation off-loop and wait through request cancellation.

    The worker owns enrollment. Cancelling the request must not start
    staging cleanup while replace or write is still in the helper thread.
    Repeated cancellation keeps waiting until the worker finishes.
    """
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException as failure:
                raise cancelled from failure
        if not task.cancelled():
            if (failure := task.exception()) is not None:
                raise cancelled from failure
            if on_success is not None:
                on_success()
        raise
    if on_success is not None:
        on_success()
    return result


def write_bytes_enrolled(path: str | Path, data: bytes) -> None:
    """Overwrite ``path`` with a native handle enrolled before open."""
    target = Path(path)
    lease = acquire_legacy(target, "files_write")
    write_error: BaseException | None = None
    close_error: BaseException | None = None
    released = False
    fh = None
    try:
        fh = open(target, "wb")
        released = False
        fh.write(data)
    except BaseException as exc:
        write_error = exc
    finally:
        if fh is not None:
            try:
                fh.close()
                released = True
            except BaseException as exc:
                close_error = exc
                released = False
        lease.finish(source_released=released)
    if write_error is not None:
        if close_error is not None:
            raise write_error from close_error
        raise write_error
    if close_error is not None:
        raise close_error


def replace_enrolled(src: str | Path, dst: str | Path) -> None:
    """Publish ``src`` onto ``dst`` while occupying both exact identities.

    Handle-free: occupancy is released after ``os.replace`` returns, even
    when the syscall fails. Family mapping is not inferred from suffixes.
    """
    source = Path(src)
    target = Path(dst)
    target_lease = acquire_legacy(target, "files_replace")
    try:
        source_lease = acquire_legacy(source, "files_replace_src")
    except BaseException:
        target_lease.finish(source_released=True)
        raise
    try:
        os.replace(source, target)
    finally:
        source_lease.finish(source_released=True)
        target_lease.finish(source_released=True)


def unlink_enrolled(path: str | Path) -> None:
    """Unlink one exact file after occupying its original identity.

    Handle-free: occupancy is released after ``os.unlink`` returns, even
    when the syscall fails. Directory trees are out of scope here.
    """
    target = Path(path)
    lease = acquire_legacy(target, "files_delete")
    try:
        os.unlink(target)
    finally:
        lease.finish(source_released=True)


__all__ = [
    "replace_enrolled",
    "run_mutation_worker",
    "unlink_enrolled",
    "write_bytes_enrolled",
]
