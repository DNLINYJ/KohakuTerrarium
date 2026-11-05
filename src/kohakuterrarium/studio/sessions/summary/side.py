"""Session-summary titles for persisted user-audience side conversations."""

import json
from collections.abc import Callable
from typing import Any

from kohakuterrarium.studio.identity.session_summary import OFF, SummarySettings
from kohakuterrarium.studio.persistence.session_index.exchange import flatten_text
from kohakuterrarium.studio.sessions.summary.record import USER, should_replace
from kohakuterrarium.studio.sessions.summary.writer import TURN, compose, is_due

_PRIVATE_INSTRUCTIONS = "[Private context fork instructions]"


def side_messages(raw: str, inherited: Any) -> list[dict]:
    """Only side-thread messages, excluding inherited context and fork instructions."""
    if isinstance(inherited, bool) or not isinstance(inherited, int) or inherited < 0:
        return []
    messages = json.loads(raw).get("messages", [])
    return [
        message
        for message in messages[inherited:]
        if isinstance(message, dict)
        and not flatten_text(message.get("content"), 2000)
        .lstrip()
        .startswith(_PRIVATE_INSTRUCTIONS)
    ]


def _current(meta: dict) -> dict:
    if not meta.get("title_at_turn") and meta.get("title_source") != USER:
        return {}
    return {
        "text": meta.get("title", ""),
        "source": meta.get("title_source", "heuristic"),
        "at_turn": meta.get("title_at_turn", 0),
    }


def _load(store: Any, parent: str, event: dict) -> tuple[dict, str] | None:
    meta = store.load_subagent_meta(parent, event["name"], event["run"])
    if (
        not meta
        or meta.get("audience") != "user"
        or meta.get("job_id") != event["job_id"]
        or meta.get("side_turn", 1) != event.get("side_turn", 1)
        or meta.get("status") == "running"
    ):
        return None
    raw = store.load_subagent_conversation(parent, event["name"], event["run"])
    return (meta, raw) if raw else None


def _write(
    store: Any,
    parent: str,
    event: dict,
    snapshot: tuple[dict, str],
    text: str,
    source: str,
    configured: str,
) -> dict | None:
    latest = _load(store, parent, event)
    if latest is None:
        return None
    meta, raw = latest
    previous, previous_raw = snapshot
    if (
        raw != previous_raw
        or meta.get("inherited_message_count")
        != previous.get("inherited_message_count")
        or not should_replace(_current(meta), source, configured)
    ):
        return None
    meta = {
        **meta,
        "title": text,
        "title_source": source,
        "title_at_turn": event.get("side_turn", 1),
    }
    store.save_subagent(parent, event["name"], event["run"], meta)
    return meta


async def summarize_side(
    store: Any,
    settings: SummarySettings,
    *,
    parent: str,
    event: dict,
    llm_factory: Callable[[], Any],
) -> dict | None:
    """Refresh one side title with the session summary source, model and cadence."""
    if settings.source == OFF:
        return None
    snapshot = await store.run(_load, store, parent, event)
    if snapshot is None:
        return None
    meta, raw = snapshot
    current = _current(meta)
    if meta.get("title_source") == USER or not is_due(
        current, event.get("side_turn", 1), settings.every_n_turns, TURN
    ):
        return None
    messages = side_messages(raw, meta.get("inherited_message_count"))
    if not messages:
        return None
    text, source = await compose(settings.source, messages, "", llm_factory)
    if not text or not should_replace(current, source, settings.source):
        return None
    return await store.run(
        _write, store, parent, event, snapshot, text, source, settings.source
    )
