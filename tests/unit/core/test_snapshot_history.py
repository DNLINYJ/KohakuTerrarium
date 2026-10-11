"""Checkpoint selection and dependency-safe prefix reconstruction."""

from pathlib import Path

import pytest

from kohakuterrarium.core.agent_message_history import (
    replay_with_settled_tools,
    snapshot_needs_settled_tool_replay,
)
from kohakuterrarium.core.snapshot_history import prepare_snapshot
from kohakuterrarium.session.store import SessionStore

_EVENTS = [
    {
        "event_id": 1,
        "type": "user_message",
        "content": "raw_u1",
        "turn_index": 1,
        "branch_id": 1,
    },
    {
        "event_id": 2,
        "type": "text",
        "content": "raw_a1",
        "turn_index": 1,
        "branch_id": 1,
    },
    {
        "event_id": 3,
        "type": "user_message",
        "content": "raw_u2_a",
        "turn_index": 2,
        "branch_id": 1,
        "parent_branch_path": [[1, 1]],
    },
    {
        "event_id": 4,
        "type": "text",
        "content": "raw_a2_a",
        "turn_index": 2,
        "branch_id": 1,
        "parent_branch_path": [[1, 1]],
    },
    {
        "event_id": 5,
        "type": "user_message",
        "content": "raw_u2_b",
        "turn_index": 2,
        "branch_id": 2,
        "parent_branch_path": [[1, 1]],
    },
    {
        "event_id": 6,
        "type": "text",
        "content": "raw_a2_b",
        "turn_index": 2,
        "branch_id": 2,
        "parent_branch_path": [[1, 1]],
    },
]


def test_prepare_snapshot_exact_and_prefix_trim(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "s.kohakutr"))
    msgs_a = [
        {
            "role": "user",
            "content": "snap_u1",
            "metadata": {"turn_index": 1, "branch_id": 1},
        },
        {"role": "assistant", "content": "snap_a1"},
        {
            "role": "user",
            "content": "snap_u2_a",
            "metadata": {"turn_index": 2, "branch_id": 1},
        },
    ]
    msgs_a.append({"role": "assistant", "content": "snap_a2"})
    store.save_branch_snapshot(
        "alice",
        msgs_a,
        branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
        watermark=4,
    )
    store.conversation["alice"] = [{"role": "user", "content": "latest_B"}]

    exact = prepare_snapshot(
        store,
        "alice",
        _EVENTS,
        selected={1: 1, 2: 1},
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
    )
    assert exact is not None and exact["selected"] == {1: 1, 2: 1}
    assert [m["content"] for m in exact["messages"]] == [
        "snap_u1",
        "snap_a1",
        "snap_u2_a",
        "snap_a2",
    ]

    trimmed = prepare_snapshot(
        store,
        "alice",
        _EVENTS,
        selected={1: 1, 2: 2},
        before_turn=2,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
    )
    assert trimmed is not None and [m["content"] for m in trimmed["messages"]] == [
        "snap_u1",
        "snap_a1",
    ]
    for kind in ("text_chunk", "text", "assistant_reasoning"):
        later = _EVENTS + [
            {
                "event_id": 7,
                "type": kind,
                "content": "later fragment",
                "reasoning_content": "later reasoning",
                "turn_index": 2,
                "branch_id": 1,
                "parent_branch_path": [[1, 1]],
            }
        ]
        assert (
            prepare_snapshot(
                store,
                "alice",
                later,
                {1: 1, 2: 1},
                replay=replay_with_settled_tools,
                needs_replay=snapshot_needs_settled_tool_replay,
            )
            is None
        )
        replayed = replay_with_settled_tools(later, branch_view={1: 1, 2: 1})
        if kind == "assistant_reasoning":
            assert replayed[-1]["reasoning_content"] == "later reasoning"
        else:
            assert replayed[-1]["content"] == "raw_a2_alater fragment"
    consumed = _EVENTS + [
        {
            "event_id": 7,
            "type": "tool_call",
            "call_id": "ancestor",
            "name": "echo",
            "args": {},
            "turn_index": 1,
            "branch_id": 1,
        },
        {
            "event_id": 8,
            "type": "tool_result",
            "call_id": "ancestor",
            "name": "echo",
            "output": "old inherited result",
            "turn_index": 2,
            "branch_id": 2,
            "parent_branch_path": [[1, 1]],
        },
    ]
    empty = [{"role": "system", "content": "system"}]
    store.save_branch_snapshot(
        "alice",
        empty,
        branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
        watermark=8,
    )
    prepared = prepare_snapshot(
        store,
        "alice",
        consumed,
        {1: 1, 2: 1},
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
    )
    assert prepared["messages"] == empty
    for messages in (
        [],
        empty
        + [
            {
                "role": "user",
                "content": "foreign",
                "metadata": {"turn_index": 2, "branch_id": 2},
            }
        ],
    ):
        store.save_branch_snapshot(
            "alice",
            messages,
            branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
            watermark=8,
        )
        outcome = prepare_snapshot(
            store,
            "alice",
            consumed,
            {1: 1, 2: 1},
            replay=replay_with_settled_tools,
            needs_replay=snapshot_needs_settled_tool_replay,
        )
        assert outcome["messages"] == [] if messages == [] else outcome is None
    store.save_branch_snapshot(
        "alice",
        empty,
        branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
        watermark=8,
    )
    assert (
        prepare_snapshot(
            store,
            "alice",
            consumed,
            {1: 1, 2: 2},
            before_turn=2,
            replay=replay_with_settled_tools,
            needs_replay=snapshot_needs_settled_tool_replay,
        )
        is None
    )
    continued = consumed + [
        {
            "event_id": 9,
            "type": "user_message",
            "content": "NEW1",
            "turn_index": 3,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1]],
        },
        {
            "event_id": 10,
            "type": "text_chunk",
            "content": "new answer",
            "turn_index": 3,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1]],
        },
        {
            "event_id": 11,
            "type": "user_message",
            "content": "NEW2",
            "turn_index": 4,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
        },
    ]
    retained = empty + [
        {
            "role": "user",
            "content": "NEW1",
            "metadata": {"turn_index": 3, "branch_id": 1},
        },
        {"role": "assistant", "content": "new answer"},
        {
            "role": "user",
            "content": "NEW2",
            "metadata": {"turn_index": 4, "branch_id": 1},
        },
    ]
    store.save_branch_snapshot(
        "alice",
        retained,
        branch={
            "turn_index": 4,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
            "context_cutoff_event_id": 8,
        },
        watermark=11,
    )
    trimmed = prepare_snapshot(
        store,
        "alice",
        continued,
        {1: 1, 2: 1, 3: 1, 4: 1},
        before_turn=4,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
    )
    assert trimmed["messages"] == retained[:-1]
    assert trimmed["context_cutoff_event_id"] == 8
    later = continued + [
        {
            "event_id": 12,
            "type": "tool_call",
            "call_id": "newjob",
            "name": "echo",
            "args": {},
            "turn_index": 3,
            "branch_id": 1,
            "parent_branch_path": [[1, 1], [2, 1]],
        },
        {
            "event_id": 13,
            "type": "tool_result",
            "call_id": "newjob",
            "name": "echo",
            "output": "new final result",
            "turn_index": 4,
            "branch_id": 2,
            "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
        },
    ]
    for pending in (False, True):
        ack = retained + (
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "newjob",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "newjob",
                    "name": "echo",
                    "content": "pending",
                    "metadata": {"job_id": "newjob", "tool_result_phase": "pending"},
                },
            ]
            if pending
            else []
        )
        store.save_branch_snapshot(
            "alice",
            ack,
            branch={
                "turn_index": 4,
                "branch_id": 1,
                "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
                "context_cutoff_event_id": 8,
            },
            watermark=13,
        )
        repaired = prepare_snapshot(
            store,
            "alice",
            later,
            {1: 1, 2: 1, 3: 1, 4: 1},
            replay=replay_with_settled_tools,
            needs_replay=snapshot_needs_settled_tool_replay,
        )
        assert [
            msg["content"] for msg in repaired["messages"] if msg["role"] == "user"
        ] == ["NEW1", "NEW2"]
        assert [
            msg["content"] for msg in repaired["messages"] if msg["role"] == "tool"
        ] == ["new final result"]
        assert repaired["context_cutoff_event_id"] == 8
        target_job = later + [
            {
                "event_id": 14,
                "type": "tool_call",
                "call_id": "target-job",
                "name": "echo",
                "args": {},
                "turn_index": 4,
                "branch_id": 1,
                "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
            },
            {
                "event_id": 15,
                "type": "tool_result",
                "call_id": "target-job",
                "name": "echo",
                "output": "excluded target result",
                "turn_index": 4,
                "branch_id": 1,
                "parent_branch_path": [[1, 1], [2, 1], [3, 1]],
            },
        ]
        prior = prepare_snapshot(
            store,
            "alice",
            target_job,
            {1: 1, 2: 1, 3: 1, 4: 1},
            before_turn=4,
            replay=replay_with_settled_tools,
            needs_replay=snapshot_needs_settled_tool_replay,
        )
        assert [
            msg["content"] for msg in prior["messages"] if msg["role"] == "user"
        ] == ["NEW1"]
        assert [
            msg["content"] for msg in prior["messages"] if msg["role"] == "tool"
        ] == ["new final result"]
        assert prior["context_cutoff_event_id"] == 8
    store.close()


@pytest.mark.parametrize(
    ("compact_source", "before_turn", "selected", "expect_ok"),
    [
        (None, None, {1: 1, 2: 1}, True),
        (None, 2, {1: 1, 2: 2}, False),
        ([[1, 1]], 2, {1: 1, 2: 2}, True),
        ([[1, 1], [2, 1]], 2, {1: 1, 2: 2}, False),
    ],
)
def test_prepare_snapshot_summary_lineage_matrix(
    tmp_path: Path, compact_source, before_turn, selected, expect_ok
) -> None:
    store = SessionStore(str(tmp_path / "s.kohakutr"))
    meta = {"compact_summary": True}
    if compact_source is not None:
        meta["compact_source_path"] = compact_source
    msgs = [
        {
            "role": "assistant",
            "content": "[Previous context summary (compact round 1)] s",
            "metadata": meta,
        },
        {
            "role": "user",
            "content": "snap_u2_a",
            "metadata": {"turn_index": 2, "branch_id": 1},
        },
    ]
    store.save_branch_snapshot(
        "alice",
        msgs,
        branch={"turn_index": 2, "branch_id": 1, "parent_branch_path": [[1, 1]]},
        watermark=4,
    )
    res = prepare_snapshot(
        store,
        "alice",
        _EVENTS,
        selected=selected,
        before_turn=before_turn,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
    )
    assert (res is not None) == expect_ok
    store.close()
