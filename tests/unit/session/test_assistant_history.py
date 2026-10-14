"""Unit tests for :mod:`kohakuterrarium.session.assistant_history`."""

from copy import deepcopy

from kohakuterrarium.session.assistant_history import project_assistant_rounds


def _evt(event_id, etype, **kwargs):
    event = {
        "event_id": event_id,
        "type": etype,
        "turn_index": kwargs.pop("turn_index", 1),
        "branch_id": kwargs.pop("branch_id", 1),
        "parent_branch_path": kwargs.pop("parent_branch_path", []),
    }
    event.update(kwargs)
    return event


def _message(**kwargs):
    message = {"role": "assistant", "content": kwargs.pop("content", "ok")}
    message.update(kwargs)
    return message


def _ids(events):
    return [evt.get("event_id") for evt in events]


def _types(events):
    return [evt.get("type") for evt in events]


class TestProjectAssistantRounds:
    def test_text_mode_synthetic_declaration_is_not_suppressed(self):
        events = [
            _evt(1, "assistant_round_start"),
            {
                "type": "assistant_tool_calls",
                "tool_calls": [
                    {"id": "textcall", "function": {"name": "echo", "arguments": "{}"}}
                ],
            },
            _evt(
                2,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="[/echo]hello[echo/]"),
            ),
        ]
        assert project_assistant_rounds(events)[1]["type"] == "assistant_tool_calls"

    def test_empty(self):
        assert project_assistant_rounds([]) == []

    def test_ordered_content_media_extras_toolcalls_detached(self):
        media = {"type": "image_url", "image_url": {"url": "https://img"}}
        extra = {"source": "model"}
        tool_calls = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }
        ]
        end_message = _message(
            content=[
                {"type": "text", "text": "hello"},
                media,
            ],
            tool_calls=tool_calls,
            reasoning_content="think",
            extra_field=extra,
        )
        events = [
            _evt(1, "user_message", content="q"),
            _evt(2, "assistant_round_start"),
            _evt(3, "text_chunk", content="hel"),
            _evt(4, "assistant_image", url="https://img"),
            _evt(5, "assistant_reasoning", reasoning_content="think"),
            _evt(
                6,
                "assistant_tool_calls",
                tool_calls=tool_calls,
            ),
            _evt(
                7,
                "assistant_message",
                round_start_event_id=2,
                message=end_message,
            ),
        ]
        original = deepcopy(events)

        out = project_assistant_rounds(events)

        assert events == original
        assert _ids(out) == [1, 2, 3, 4, 5, 6, 7]
        assert _types(out) == [
            "user_message",
            "assistant_message",
            "assistant_round_display",
            "assistant_round_display",
            "assistant_round_display",
            "assistant_round_display",
            "assistant_round_end",
        ]
        projected = out[1]
        assert projected["_assistant_projected"] is True
        assert projected["canonical_source_event_id"] == 7
        assert projected["event_id"] == 2
        assert projected["message"] == end_message
        assert projected["message"] is not events[6]["message"]
        assert projected["message"]["content"] is not events[6]["message"]["content"]
        assert (
            projected["message"]["tool_calls"] is not events[6]["message"]["tool_calls"]
        )
        assert (
            projected["message"]["extra_field"]
            is not events[6]["message"]["extra_field"]
        )

        projected["message"]["content"][0]["text"] = "mutated"
        projected["message"]["extra_field"]["source"] = "changed"
        assert events[6]["message"]["content"][0]["text"] == "hello"
        assert events[6]["message"]["extra_field"]["source"] == "model"
        assert out[6]["message"]["content"][0]["text"] == "hello"

    def test_tool_result_before_end_is_projected_before_result(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="calling"),
            _evt(3, "assistant_tool_calls", tool_calls=[{"id": "c1"}]),
            _evt(4, "tool_call", call_id="c1", name="bash"),
            _evt(5, "tool_result", call_id="c1", output="done"),
            _evt(
                6,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="", tool_calls=[{"id": "c1"}]),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_message",
            "assistant_round_display",
            "assistant_round_display",
            "tool_call",
            "tool_result",
            "assistant_round_end",
        ]
        assert out[0]["_assistant_projected"] is True
        assert out[0]["message"]["tool_calls"] == [{"id": "c1"}]
        assert _ids(out).index(6) > _ids(out).index(5)
        assert out[0]["event_id"] == 1
        assert out[4]["type"] == "tool_result"

    def test_two_rounds_same_turn(self):
        events = [
            _evt(1, "user_message", content="q"),
            _evt(2, "assistant_round_start"),
            _evt(3, "text_chunk", content="first"),
            _evt(4, "assistant_tool_calls", tool_calls=[{"id": "c1"}]),
            _evt(5, "tool_call", call_id="c1", name="bash"),
            _evt(6, "tool_result", call_id="c1", output="r"),
            _evt(
                7,
                "assistant_message",
                round_start_event_id=2,
                message=_message(content="", tool_calls=[{"id": "c1"}]),
            ),
            _evt(8, "assistant_round_start"),
            _evt(9, "text_chunk", content="final"),
            _evt(
                10,
                "assistant_message",
                round_start_event_id=8,
                message=_message(content="final"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "user_message",
            "assistant_message",
            "assistant_round_display",
            "assistant_round_display",
            "tool_call",
            "tool_result",
            "assistant_round_end",
            "assistant_message",
            "assistant_round_display",
            "assistant_round_end",
        ]
        assert out[1]["canonical_source_event_id"] == 7
        assert out[7]["canonical_source_event_id"] == 10
        assert out[1]["message"]["tool_calls"] == [{"id": "c1"}]
        assert out[7]["message"]["content"] == "final"
        assert _ids(out) == list(range(1, 11))

    def test_foreign_branch_pair_does_not_apply(self):
        events = [
            _evt(1, "assistant_round_start", branch_id=1),
            _evt(2, "text_chunk", content="keep", branch_id=1),
            _evt(
                3,
                "assistant_message",
                branch_id=2,
                round_start_event_id=1,
                message=_message(content="other-branch"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_round_start",
            "text_chunk",
            "assistant_message",
        ]
        assert "_assistant_projected" not in out[0]
        assert out[1]["content"] == "keep"

    def test_foreign_parent_path_pair_does_not_apply(self):
        events = [
            _evt(1, "assistant_round_start", parent_branch_path=[[0, 1]]),
            _evt(2, "text_chunk", content="keep", parent_branch_path=[[0, 1]]),
            _evt(
                3,
                "assistant_message",
                parent_branch_path=[[0, 2]],
                round_start_event_id=1,
                message=_message(content="other-path"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_round_start",
            "text_chunk",
            "assistant_message",
        ]

    def test_interrupted_start_fallback_keeps_display(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="partial"),
            _evt(3, "assistant_reasoning", reasoning_content="mid"),
            _evt(4, "user_message", content="next"),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_round_start",
            "text_chunk",
            "assistant_reasoning",
            "user_message",
        ]
        assert out[1]["content"] == "partial"

    def test_duplicate_ends_preserve_legacy_display(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="legacy"),
            _evt(
                3,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="first"),
            ),
            _evt(
                4,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="second"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_round_start",
            "text_chunk",
            "assistant_message",
            "assistant_message",
        ]
        assert out[1]["content"] == "legacy"
        assert out[2]["message"]["content"] == "first"
        assert out[3]["message"]["content"] == "second"

    def test_overlapping_next_start_does_not_project(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="one"),
            _evt(3, "assistant_round_start"),
            _evt(4, "text_chunk", content="two"),
            _evt(
                5,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="late"),
            ),
            _evt(
                6,
                "assistant_message",
                round_start_event_id=3,
                message=_message(content="inner"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert out[0]["type"] == "assistant_round_start"
        assert out[1]["type"] == "text_chunk"
        assert out[4]["type"] == "assistant_message"
        assert out[2]["type"] == "assistant_message"
        assert out[3]["type"] == "assistant_round_display"
        assert out[5]["type"] == "assistant_round_end"
        assert out[2]["canonical_source_event_id"] == 6

    def test_user_message_between_does_not_project(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="keep"),
            _evt(3, "user_message", content="interrupt"),
            _evt(
                4,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="stale"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_round_start",
            "text_chunk",
            "user_message",
            "assistant_message",
        ]

    def test_bool_and_non_positive_ids_are_ignored(self):
        events = [
            _evt(True, "assistant_round_start"),
            _evt(2, "text_chunk", content="a"),
            _evt(
                3,
                "assistant_message",
                round_start_event_id=True,
                message=_message(content="bool-start"),
            ),
            _evt(0, "assistant_round_start"),
            _evt(5, "text_chunk", content="b"),
            _evt(
                6,
                "assistant_message",
                round_start_event_id=0,
                message=_message(content="zero-start"),
            ),
            _evt(7, "assistant_round_start"),
            _evt(8, "text_chunk", content="c"),
            _evt(
                False,
                "assistant_message",
                round_start_event_id=7,
                message=_message(content="bool-end"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert "assistant_round_display" not in _types(out)
        assert out[0]["type"] == "assistant_round_start"
        assert out[3]["type"] == "assistant_round_start"
        assert out[6]["type"] == "assistant_round_start"
        assert out[2]["type"] == "assistant_message"
        assert out[5]["type"] == "assistant_message"
        assert out[8]["type"] == "assistant_message"

    def test_eligible_partial_coverage_fallback(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="keep"),
            _evt(
                3,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="full"),
            ),
        ]

        partial_start = project_assistant_rounds(events, eligible_ids={1})
        partial_end = project_assistant_rounds(events, eligible_ids={3})
        none_eligible = project_assistant_rounds(events, eligible_ids=set())
        full = project_assistant_rounds(events, eligible_ids={1, 2, 3})

        assert _types(partial_start) == [
            "assistant_round_start",
            "text_chunk",
            "assistant_message",
        ]
        assert _types(partial_end) == _types(partial_start)
        assert _types(none_eligible) == _types(partial_start)
        assert _types(full) == [
            "assistant_message",
            "assistant_round_display",
            "assistant_round_end",
        ]

    def test_retains_ids_and_monotonic_order(self):
        events = [
            _evt(1, "user_message", content="q"),
            _evt(2, "assistant_round_start"),
            _evt(3, "text_chunk", content="a"),
            _evt(4, "activity:thinking", name="think"),
            _evt(
                5,
                "assistant_message",
                round_start_event_id=2,
                message=_message(content="a"),
            ),
            _evt(6, "assistant_round_start"),
            _evt(7, "assistant_text", content="b"),
            _evt(
                8,
                "assistant_message",
                round_start_event_id=6,
                message=_message(content="b"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert len(out) == len(events)
        assert _ids(out) == _ids(events) == [1, 2, 3, 4, 5, 6, 7, 8]
        assert out[3]["type"] == "activity:thinking"
        assert [evt["event_id"] for evt in out] == sorted(
            evt["event_id"] for evt in out
        )

    def test_idempotent_reapplication_keeps_projected_anchor(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text_chunk", content="hello"),
            _evt(
                3,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="hello", reasoning_content="r"),
            ),
        ]

        first = project_assistant_rounds(events)
        second = project_assistant_rounds(first)

        assert _types(first) == _types(second)
        assert _ids(first) == _ids(second)
        assert second[0]["_assistant_projected"] is True
        assert second[0]["canonical_source_event_id"] == 3
        assert second[0]["message"] == first[0]["message"]
        assert second[0] is not first[0]
        assert second[0]["message"] is not first[0]["message"]
        second[0]["message"]["content"] = "mutated"
        assert first[0]["message"]["content"] == "hello"

    def test_already_projected_list_unchanged_and_detached(self):
        message = _message(content=["part"], extra={"k": "v"})
        events = [
            {
                "event_id": 1,
                "type": "assistant_message",
                "turn_index": 1,
                "branch_id": 1,
                "parent_branch_path": [],
                "message": message,
                "canonical_source_event_id": 3,
                "_assistant_projected": True,
            },
            _evt(2, "assistant_round_display", content="hello"),
            _evt(3, "assistant_round_end", round_start_event_id=1, message=message),
        ]
        original_types = _types(events)

        out = project_assistant_rounds(events)

        assert _types(out) == original_types
        assert out[0]["_assistant_projected"] is True
        assert out[0]["canonical_source_event_id"] == 3
        assert out[0]["message"] == message
        assert out[0]["message"] is not message
        assert out[0]["message"]["extra"] is not message["extra"]
        assert out[2]["message"] is not out[0]["message"]
        out[0]["message"]["extra"]["k"] = "changed"
        assert events[0]["message"]["extra"]["k"] == "v"
        assert events[2]["message"]["extra"]["k"] == "v"

    def test_parent_path_list_and_tuple_are_same_lineage(self):
        events = [
            _evt(1, "assistant_round_start", parent_branch_path=[[0, 1]]),
            _evt(2, "text_chunk", content="x", parent_branch_path=[[0, 1]]),
            _evt(
                3,
                "assistant_message",
                parent_branch_path=((0, 1),),
                round_start_event_id=1,
                message=_message(content="x"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert out[0]["type"] == "assistant_message"
        assert out[0]["_assistant_projected"] is True
        assert out[2]["type"] == "assistant_round_end"

    def test_keeps_non_display_events_inside_projected_round(self):
        events = [
            _evt(1, "assistant_round_start"),
            _evt(2, "text", content="stream"),
            _evt(3, "assistant_text", content="alias"),
            _evt(4, "tool_call", call_id="c1", name="bash"),
            _evt(5, "tool_result", call_id="c1", output="ok"),
            _evt(6, "activity:tool", name="bash"),
            _evt(7, "processing_start"),
            _evt(
                8,
                "assistant_message",
                round_start_event_id=1,
                message=_message(content="stream"),
            ),
        ]

        out = project_assistant_rounds(events)

        assert _types(out) == [
            "assistant_message",
            "assistant_round_display",
            "assistant_round_display",
            "tool_call",
            "tool_result",
            "activity:tool",
            "processing_start",
            "assistant_round_end",
        ]
