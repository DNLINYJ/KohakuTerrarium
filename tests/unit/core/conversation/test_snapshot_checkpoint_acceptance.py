"""Checkpoints a resume must accept instead of falling back to a full replay."""

import copy
from pathlib import Path

import pytest

from kohakuterrarium.core.agent.history.message_history import (
    replay_with_settled_tools,
    snapshot_needs_settled_tool_replay,
)
from kohakuterrarium.core.conversation.settled_identity import (
    delivered_jobs,
    result_identities,
)
from kohakuterrarium.core.conversation.snapshot import prepare_snapshot
from kohakuterrarium.session.store import SessionStore

_SUMMARY = {
    "role": "assistant",
    "content": "[Previous context summary (compact round 1)] earlier work",
    "metadata": {"compact_summary": True},
}


@pytest.fixture
def store(tmp_path: Path):
    s = SessionStore(str(tmp_path / "s.kohakutr"))
    try:
        yield s
    finally:
        s.close()


def _prepare(
    store: SessionStore,
    events: list[dict],
    selected: dict[int, int],
    *,
    resume: bool = True,
):
    return prepare_snapshot(
        store,
        "alice",
        events,
        selected,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
        keep_delivered_pending=resume,
    )


def _event(eid: int, etype: str, turn: int, parent=None, **fields) -> dict:
    event = {"event_id": eid, "type": etype, "turn_index": turn, "branch_id": 1}
    if parent:
        event["parent_branch_path"] = parent
    return {**event, **fields}


def _user(content: str, turn: int | None = None, **meta) -> dict:
    if turn is not None:
        meta = {"turn_index": turn, "branch_id": 1, **meta}
    return {"role": "user", "content": content, "metadata": meta}


_BACKGROUND_EVENTS = [
    _event(1, "user_message", 1, content="run the tunnel"),
    _event(2, "tool_call", 1, call_id="bg", name="bash", args={}),
    _event(3, "tool_result", 1, call_id="bg", name="bash", output="tunnel up"),
    _event(4, "text", 1, content="tunnel is up"),
]


def _background_messages(delivered: bool) -> list[dict]:
    messages = [
        _user("run the tunnel", 1),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "bg",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "bg",
            "name": "bash",
            "content": "[bash] Running in background — task delegated.",
            "metadata": {"job_id": "bg", "tool_result_phase": "pending"},
        },
    ]
    if delivered:
        messages.append(_user("[Tool bg completed]\ntunnel up", origin_known=False))
    return messages + [{"role": "assistant", "content": "tunnel is up"}]


@pytest.mark.parametrize(
    ("delivered", "resume", "accepted"),
    [(True, True, True), (False, True, True), (True, False, False)],
)
def test_background_result_delivered_in_checkpoint_keeps_it(
    store: SessionStore, delivered: bool, resume: bool, accepted: bool
) -> None:
    messages = _background_messages(delivered)
    store.save_branch_snapshot(
        "alice",
        copy.deepcopy(messages),
        branch={"turn_index": 1, "branch_id": 1, "parent_branch_path": []},
        watermark=4,
    )

    prepared = _prepare(store, copy.deepcopy(_BACKGROUND_EVENTS), {1: 1}, resume=resume)

    if not accepted:
        # Without the delivery the checkpoint lacks the result; a truncating
        # caller could cut the delivery and keep only the placeholder.
        assert prepared is None
        return
    if not delivered:
        assert prepared["messages"][2]["content"] == "tunnel up"
        assert prepared["messages"][2]["metadata"]["tool_result_phase"] == "final"
        assert prepared["messages"][:2] == messages[:2]
        assert prepared["messages"][3:] == messages[3:]
        assert store.load_conversation("alice") == messages
        return
    assert [m["content"] for m in prepared["messages"]] == [
        m["content"] for m in messages
    ]


@pytest.mark.parametrize("marker", [(0, 0), (0, 1), (1, 0), (-1, 1)])
def test_exact_checkpoint_keeps_pre_turn_peer_messages(store, marker):
    messages = [
        _SUMMARY,
        _user("peer task", marker[0], branch_id=marker[1], origin_known=True),
        {"role": "assistant", "content": "peer task finished"},
        _user("resume work", 1),
        {"role": "assistant", "content": "latest work"},
    ]
    store.save_branch_snapshot(
        "alice",
        messages,
        branch={"turn_index": 1, "branch_id": 1, "parent_branch_path": []},
        watermark=2,
    )
    events = [
        _event(1, "user_message", 1, content="resume work"),
        _event(2, "text", 1, content="latest work"),
    ]
    prepared = _prepare(store, events, {1: 1})
    assert prepared["messages"] == messages
    assert store.load_conversation("alice") == messages


def test_completed_slot_in_a_later_turn_keeps_its_checkpoint(store):
    later = _background_messages(False)
    later[0]["metadata"]["turn_index"] = 2
    messages = [
        _user("first task", 1),
        {"role": "assistant", "content": "first done"},
        *later,
    ]
    events = [
        _event(1, "user_message", 1, content="first task"),
        _event(2, "text", 1, content="first done"),
    ]
    events.extend(
        {
            **event,
            "event_id": event["event_id"] + 2,
            "turn_index": 2,
            "parent_branch_path": [[1, 1]],
        }
        for event in _BACKGROUND_EVENTS
    )
    store.save_branch_snapshot(
        "alice",
        messages,
        branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
        watermark=6,
    )
    prepared = _prepare(store, events, {1: 1, 2: 1})
    assert prepared is not None
    assert len(prepared["messages"]) == len(messages)
    assert prepared["messages"][4]["content"] == "tunnel up"
    assert prepared["messages"][4]["metadata"]["result_event_id"] == 5


@pytest.mark.parametrize("mode", ["other_branch", "truncate"])
def test_unknown_origin_checkpoint_is_not_reused_on_divergence(store, mode):
    messages = [
        _SUMMARY,
        _user("private peer work", 0, branch_id=0),
        _user("original input", 1),
        {"role": "assistant", "content": "original answer"},
    ]
    store.save_branch_snapshot(
        "alice",
        messages,
        branch={"turn_index": 1, "branch_id": 1, "parent_branch_path": []},
        watermark=2,
    )
    events = [
        _event(1, "user_message", 1, content="original input"),
        _event(2, "text", 1, content="original answer"),
        {**_event(3, "user_message", 1, content="edited input"), "branch_id": 2},
    ]
    prepared = prepare_snapshot(
        store,
        "alice",
        events,
        {1: 2} if mode == "other_branch" else {1: 1},
        before_turn=1 if mode == "truncate" else None,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
        keep_delivered_pending=True,
    )
    assert prepared is None


@pytest.mark.parametrize(
    "unsafe",
    [
        "unselected_call",
        "missing_call",
        "wrong_provider_id",
        "non_assistant_announcement",
        "duplicate_call",
        "duplicate_result",
        "after_watermark",
        "covered_result",
    ],
)
def test_pending_result_repair_requires_unambiguous_checkpoint_evidence(store, unsafe):
    messages = [_SUMMARY, *_background_messages(False)]
    events = copy.deepcopy(_BACKGROUND_EVENTS)
    if unsafe == "unselected_call":
        events[1]["branch_id"] = 2
    elif unsafe == "missing_call":
        events.pop(1)
    elif unsafe == "wrong_provider_id":
        messages[3]["tool_call_id"] = "wrong"
    elif unsafe == "non_assistant_announcement":
        messages[2]["role"] = "user"
    elif unsafe == "duplicate_call":
        events.insert(2, {**events[1], "event_id": 5})
    elif unsafe == "duplicate_result":
        events.insert(3, {**events[2], "event_id": 5})
    elif unsafe == "after_watermark":
        events[2]["event_id"] = 6
    elif unsafe == "covered_result":
        events.append(
            _event(
                5,
                "compact_complete",
                1,
                summary="older work",
                round=1,
                replaced_from_event_id=3,
                replaced_to_event_id=3,
                compact_path=[[1, 1]],
                compact_result_event_ids=[3],
            )
        )
    store.save_branch_snapshot(
        "alice",
        messages,
        branch={"turn_index": 1, "branch_id": 1, "parent_branch_path": []},
        watermark=(
            5
            if unsafe in {"duplicate_call", "duplicate_result", "covered_result"}
            else 4
        ),
    )
    prepared = _prepare(store, events, {1: 1})
    if prepared is not None:
        assert prepared["messages"][3] == messages[3]
    assert store.load_conversation("alice") == messages


def _userless_events(turn_two: str) -> list[dict]:
    events = [
        _event(1, "user_message", 1, content="u1"),
        _event(2, "text", 1, content="a1"),
    ]
    if turn_two == "typed":
        events.append(_event(3, "user_message", 2, [[1, 1]], content="typed"))
    if turn_two in ("typed", "triggered"):
        if turn_two == "triggered":
            events.append(_event(3, "trigger_fired", 2, [[1, 1]]))
        events.append(_event(4, "text", 2, [[1, 1]], content="a2"))
    return events + [
        _event(5, "user_message", 3, [[1, 1], [2, 1]], content="u3"),
        _event(6, "text", 3, [[1, 1], [2, 1]], content="a3"),
    ]


@pytest.mark.parametrize("turn_two", ["triggered", "unrecorded", "typed"])
def test_unselected_userless_turn_does_not_break_an_exact_match(
    store: SessionStore, turn_two: str
) -> None:
    messages = [
        _SUMMARY,
        _user("u1", 1),
        {"role": "assistant", "content": "a1"},
        _user("u3", 3),
        {"role": "assistant", "content": "a3"},
    ]
    store.save_branch_snapshot(
        "alice",
        copy.deepcopy(messages),
        branch={
            "turn_index": 3,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1]],
        },
        watermark=6,
    )

    prepared = _prepare(store, _userless_events(turn_two), {1: 1, 3: 1})

    if turn_two == "typed":
        # A typed turn the view skips is a real divergence.
        assert prepared is None
    else:
        assert prepared is not None
        assert [m["content"] for m in prepared["messages"]] == [
            m["content"] for m in messages
        ]


_CONTINUED_EVENTS = [
    _event(1, "user_message", 1, content="u1"),
    _event(2, "text", 1, content="a1"),
    _event(3, "user_message", 2, [[1, 1]], content="u2"),
    _event(4, "text", 2, [[1, 1]], content="a2"),
    {**_event(5, "user_message", 1, content="u1 edited"), "branch_id": 2},
]


@pytest.mark.parametrize(
    ("selected", "accepted"), [({1: 1, 2: 1}, True), ({1: 2}, False)]
)
def test_checkpoint_continued_by_later_turns_keeps_its_prefix(
    store: SessionStore, selected: dict[int, int], accepted: bool
) -> None:
    messages = [
        _SUMMARY,
        _user("u1", 1),
        _user("[Channel 'assets' from opus]: note", origin_known=False),
        {"role": "assistant", "content": "a1"},
    ]
    store.save_branch_snapshot(
        "alice",
        copy.deepcopy(messages),
        branch={"turn_index": 1, "branch_id": 1, "parent_branch_path": []},
        watermark=2,
    )

    prepared = _prepare(store, copy.deepcopy(_CONTINUED_EVENTS), selected)

    if not accepted:
        # An edit of turn 1 diverges inside the checkpoint's own path.
        assert prepared is None
        return
    assert prepared is not None
    contents = [m["content"] for m in prepared["messages"]]
    assert contents[: len(messages)] == [m["content"] for m in messages]
    assert contents[len(messages) :] == ["u2", "a2"]


def test_mid_turn_batch_delivery_is_recognised() -> None:
    batch = _user(
        "[Tool bg completed]\nout\n\n[Tool other failed]\nboom", origin_known=False
    )
    mixed = _user(
        [
            {"type": "text", "text": "typed note"},
            {"type": "text", "text": "[Tool late completed]\nresult"},
        ],
        origin_known=False,
    )
    typed = _user("[Tool bg completed]\nout", 1)

    assert result_identities(batch) == {"bg", "other"}
    assert delivered_jobs([batch, mixed]) == {"bg", "other", "late"}
    # A message the user typed is never read as a tool delivery.
    assert result_identities(typed) == set()
