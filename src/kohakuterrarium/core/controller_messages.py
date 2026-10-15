"""Capture completed assistant messages and emit round output events."""

import asyncio
from copy import deepcopy
from typing import Any

from kohakuterrarium.llm.media_reference import resolve_session_image_urls
from kohakuterrarium.llm.message import ContentPart, TextPart
from kohakuterrarium.modules.output.event import OutputEvent


def _merge_text_and_parts(
    text: str, structured_parts: list[ContentPart]
) -> str | list[ContentPart]:
    """Combine streamed text with structured parts (text-first list)."""
    if not structured_parts:
        return text
    merged: list[ContentPart] = []
    if text:
        merged.append(TextPart(text=text))
    merged.extend(structured_parts)
    return merged


async def prepare_session_media(controller: Any, messages: list[dict]) -> list[dict]:
    """Resolve effective request images against the controller's session location."""
    store = controller.session_store
    if store is None or not any(
        isinstance(part, dict)
        and part.get("type") == "image_url"
        and isinstance(image := part.get("image_url"), (dict, str))
        and str(image.get("url", "") if isinstance(image, dict) else image).startswith(
            ("/api/sessions/", "file://")
        )
        for message in messages
        for part in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
    ):
        return messages
    return await asyncio.to_thread(
        resolve_session_image_urls, messages, session_path=store.path
    )


async def begin_assistant(controller: Any) -> None:
    """Clear the captured assistant message and emit round start if routed."""
    controller._completed_assistant_message = None
    router = controller.output_router
    if router is not None:
        await router.emit(OutputEvent(type="assistant_round_start", payload={}))


async def end_assistant(controller: Any) -> None:
    """Emit a detached copy of the captured assistant message after post-hooks."""
    captured = controller._completed_assistant_message
    router = controller.output_router
    if captured is None or router is None:
        return
    await router.emit(
        OutputEvent(
            type="assistant_message",
            payload={"message": deepcopy(captured.to_dict())},
        )
    )
