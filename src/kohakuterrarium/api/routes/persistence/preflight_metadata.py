"""Default-off local preflight read selection and candidate failure responses."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from kohakuterrarium.session.metadata_candidate import MetadataReadError
from kohakuterrarium.session.resource_gate import ResourceReleaseError

MetadataReader = Callable[[str | Path], dict[str, Any]]


def local_metadata_reader() -> MetadataReader | None:
    """Return no candidate until a supported serving assembly supplies a reader."""
    return None


async def read_local_preflight(operation: Callable, *args, **kwargs):
    """Run a local preflight read off-loop and preserve candidate failure semantics."""
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except (MetadataReadError, ResourceReleaseError) as exc:
        resource = isinstance(exc, ResourceReleaseError) or exc.stage == "resource"
        raise HTTPException(
            status_code=503 if resource else 500,
            detail={
                "code": (
                    "session_metadata_resource_unavailable"
                    if resource
                    else "session_metadata_read_failed"
                ),
                "message": (
                    "Session metadata resources are unavailable"
                    if resource
                    else "Session metadata could not be read"
                ),
            },
        ) from exc
