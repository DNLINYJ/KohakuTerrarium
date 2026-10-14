"""Project canonical assistant rounds onto replay event streams.

Replay consumers need a complete OpenAI-shaped ``assistant_message`` at the
round's original start so later tool results remain ordered after the
announcement. Streamed display pieces stay in place as
``assistant_round_display`` so compact/monotonic identity is preserved.
"""

import json
from copy import deepcopy
from typing import Any

from kohakuterrarium.llm.antigravity_format import replay_target
from kohakuterrarium.llm.message import ASSISTANT_STATE_KEYS, ImagePart

_DISPLAY_TYPES = frozenset(
    {
        "text_chunk",
        "text",
        "assistant_text",
        "assistant_image",
        "assistant_reasoning",
        "assistant_tool_calls",
    }
)
_BOUNDARY_TYPES = frozenset({"assistant_round_start", "user_message"})


def _positive_id(value: Any) -> int | None:
    """Return a strictly positive int id, rejecting bools and non-ints."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _lineage(event: dict[str, Any]) -> tuple[Any, Any, tuple[tuple[int, int], ...]]:
    raw_path = event.get("parent_branch_path") or ()
    path: list[tuple[int, int]] = []
    try:
        for item in raw_path:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                turn, branch = item
                if isinstance(turn, int) and isinstance(branch, int):
                    path.append((turn, branch))
    except TypeError:
        path = []
    return (event.get("turn_index"), event.get("branch_id"), tuple(path))


def _eligible(event_id: int, eligible_ids: set[int] | None) -> bool:
    return eligible_ids is None or event_id in eligible_ids


def _copy_event(event: dict[str, Any]) -> dict[str, Any]:
    copied = dict(event)
    message = event.get("message")
    if isinstance(message, dict):
        copied["message"] = deepcopy(message)
    return copied


def _has_boundary_between(
    events: list[dict[str, Any]], start_index: int, end_index: int
) -> bool:
    for between in events[start_index + 1 : end_index]:
        if between.get("type") in _BOUNDARY_TYPES:
            return True
    return False


def _find_pairs(
    events: list[dict[str, Any]],
    eligible_ids: set[int] | None,
) -> dict[int, dict[str, Any]]:
    """Map valid round-start event_id → canonical end event.

    A pair is valid only when both ids are unique, positive, share lineage,
    sit on the eligible set (when provided), and no later round-start or
    user_message sits between them. Duplicate anchors or ends are ignored so
    legacy display remains recoverable.
    """
    start_index: dict[int, int] = {}
    duplicate_starts: set[int] = set()
    for index, event in enumerate(events):
        if event.get("type") != "assistant_round_start":
            continue
        event_id = _positive_id(event.get("event_id"))
        if event_id is None:
            continue
        if event_id in start_index:
            duplicate_starts.add(event_id)
            continue
        start_index[event_id] = index

    ends_by_start: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    duplicate_end_ids: set[int] = set()
    seen_end_ids: set[int] = set()
    for index, event in enumerate(events):
        if event.get("type") != "assistant_message":
            continue
        if event.get("_assistant_projected"):
            continue
        end_id = _positive_id(event.get("event_id"))
        start_id = _positive_id(event.get("round_start_event_id"))
        if end_id is None or start_id is None:
            continue
        if end_id in seen_end_ids:
            duplicate_end_ids.add(end_id)
            continue
        seen_end_ids.add(end_id)
        ends_by_start.setdefault(start_id, []).append((index, event))

    pairs: dict[int, dict[str, Any]] = {}
    for start_id, candidates in ends_by_start.items():
        if start_id in duplicate_starts or start_id not in start_index:
            continue
        unique_ends = [
            (index, event)
            for index, event in candidates
            if _positive_id(event.get("event_id")) not in duplicate_end_ids
        ]
        if len(unique_ends) != 1:
            continue
        end_index, end = unique_ends[0]
        end_id = _positive_id(end.get("event_id"))
        if end_id is None:
            continue
        anchor_index = start_index[start_id]
        if end_index <= anchor_index:
            continue
        message = end.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), (str, list, type(None)))
        ):
            continue
        calls = message.get("tool_calls")
        if calls is not None and (
            not isinstance(calls, list)
            or not all(isinstance(call, dict) for call in calls)
        ):
            continue
        anchor = events[anchor_index]
        if _lineage(anchor) != _lineage(end):
            continue
        if not (_eligible(start_id, eligible_ids) and _eligible(end_id, eligible_ids)):
            continue
        if _has_boundary_between(events, anchor_index, end_index):
            continue
        pairs[start_id] = end

    return pairs


def project_assistant_rounds(
    events: list[dict[str, Any]],
    eligible_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Project canonical assistant rounds onto a replay event list.

    Non-mutating. Length, order, and ``event_id`` of every original event are
    preserved. Valid round starts become ``assistant_message`` copies of the
    later canonical payload; the canonical end becomes ``assistant_round_end``;
    streamed display pieces inside the pair become ``assistant_round_display``.
    Unpaired / malformed / conflicting anchors leave display pieces intact.
    """
    pairs = _find_pairs(events, eligible_ids)
    end_ids = {
        end.get("event_id")
        for end in pairs.values()
        if _positive_id(end.get("event_id")) is not None
    }
    event_index = {
        event.get("event_id"): index
        for index, event in enumerate(events)
        if _positive_id(event.get("event_id")) is not None
    }
    pair_ranges: dict[int, int] = {}
    for start_id, end in pairs.items():
        end_id = end.get("event_id")
        start_pos = event_index.get(start_id)
        end_pos = event_index.get(end_id)
        if start_pos is None or end_pos is None:
            continue
        pair_ranges[start_id] = end_pos

    projected: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        copied = _copy_event(event)
        event_id = _positive_id(event.get("event_id"))
        etype = event.get("type")

        if event_id is not None and event_id in pairs:
            end = pairs[event_id]
            copied["type"] = "assistant_message"
            copied["message"] = deepcopy(end.get("message"))
            copied["canonical_source_event_id"] = end.get("event_id")
            copied["_assistant_projected"] = True
            projected.append(copied)
            continue

        if event_id is not None and event_id in end_ids:
            copied["type"] = "assistant_round_end"
            projected.append(copied)
            continue

        if etype in _DISPLAY_TYPES:
            in_projected_round = False
            for start_id, end_pos in pair_ranges.items():
                start_pos = event_index.get(start_id)
                if start_pos is None:
                    continue
                if start_pos < index < end_pos:
                    if etype == "assistant_tool_calls":
                        canonical_calls = {
                            call.get("id")
                            for call in (
                                pairs[start_id].get("message", {}).get("tool_calls")
                                or []
                            )
                        }
                        if not {
                            call.get("id") for call in (event.get("tool_calls") or [])
                        }.issubset(canonical_calls):
                            continue
                    in_projected_round = True
                    break
            if in_projected_round:
                copied["type"] = "assistant_round_display"
        projected.append(copied)

    return projected


def _coerce_tool_args_to_json(args: Any) -> str:
    """Serialize tool arguments as provider-facing JSON strings."""
    if isinstance(args, str):
        return args
    if args is None:
        return "{}"
    try:
        return json.dumps(args)
    except (TypeError, ValueError):
        return "{}"


def replay_assistant_event(
    messages: list[dict],
    event: dict,
    include_metadata: bool,
    merge_image: bool = True,
    canonical_target: dict | None = None,
) -> None:
    """Restore a complete message, legacy image, or provider reasoning state."""
    match event["type"]:
        case "assistant_message":
            if not event.get("_assistant_projected"):
                return
            message = deepcopy(event["message"])
            if include_metadata:
                message["metadata"] = {
                    "event_id": event.get(
                        "canonical_source_event_id", event.get("event_id")
                    ),
                    "turn_index": event.get("turn_index"),
                    "branch_id": event.get("branch_id"),
                }
            messages.append(message)
        case "assistant_image":
            part = ImagePart(
                url=event.get("url", ""),
                detail=event.get("detail", "auto"),
                source_name=event.get("source_name"),
                source_type=event.get("source_type"),
            ).to_dict()
            if merge_image and messages and messages[-1].get("role") == "assistant":
                previous = messages[-1].get("content")
                parts = (
                    [{"type": "text", "text": previous}]
                    if isinstance(previous, str) and previous
                    else list(previous or [])
                )
                messages[-1]["content"] = [*parts, part]
            else:
                messages.append({"role": "assistant", "content": [part]})
        case "assistant_reasoning":
            target = next(
                (
                    message
                    for message in reversed(messages)
                    if message.get("role") == "assistant"
                ),
                None,
            )
            state = event.get("_kt_antigravity_content")
            if (
                state
                and target is canonical_target
                and target is not None
                and target.get("_kt_antigravity_content") == state
            ):
                return
            if state:
                target = replay_target(
                    messages, target, event["_kt_antigravity_content"]
                )
            if target is not None:
                for key in ASSISTANT_STATE_KEYS:
                    value = event.get(key)
                    if value not in (None, "", [], {}):
                        target[key] = value
