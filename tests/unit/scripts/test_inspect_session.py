"""Unit tests for session inspection summaries and reasoning helpers."""

import importlib.util
from pathlib import Path

import pytest

from kohakuterrarium.session.store import SessionStore

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "inspect_session.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("inspect_session", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_inspect_session = _load_script()


class TestBoundedText:
    def test_short_text_unchanged(self):
        assert _inspect_session._bounded_text("short", 10) == "short"

    def test_long_text_gets_marker(self):
        out = _inspect_session._bounded_text("abcdef", 3)
        assert out.startswith("abc")
        assert "3 more chars" in out


class TestReasoningEntries:
    def test_flat_fields(self):
        out = _inspect_session._reasoning_entries(
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "private",
                "reasoning": "plain",
            }
        )
        assert out == [
            ("reasoning_content", "private"),
            ("reasoning", "plain"),
        ]

    def test_reasoning_details_with_signature(self):
        out = _inspect_session._reasoning_entries(
            {
                "role": "assistant",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "think", "signature": "sig"}
                ],
            }
        )
        assert out == [
            ("reasoning_details[0]:reasoning.text", "think\n[signature: sig]")
        ]

    def test_anthropic_thinking_blocks(self):
        out = _inspect_session._reasoning_entries(
            {
                "role": "assistant",
                "_kt_anthropic_content": [
                    {"type": "text", "text": "answer"},
                    {"type": "thinking", "thinking": "hmm", "signature": "sig2"},
                ],
            }
        )
        assert out == [("anthropic:thinking[1]", "hmm\n[signature: sig2]")]

    def test_ordered_segments_preferred_over_flat_fields(self):
        out = _inspect_session._reasoning_entries(
            {
                "role": "assistant",
                "reasoning_content": "flat",
                "_kt_assistant_segments": [
                    {
                        "type": "reasoning",
                        "source": "reasoning_content",
                        "text": "think 1",
                    },
                    {"type": "text", "text": "answer 1"},
                    {"type": "tool_call_ref", "call_id": "call_1"},
                ],
            }
        )
        assert out == [
            ("segments[0] reasoning:reasoning_content", "think 1"),
            ("segments[1] text", "answer 1"),
            ("segments[2] tool_call_ref", "call_1"),
        ]

    def test_non_assistant_ignored_by_print_path(self):
        assert _inspect_session._reasoning_entries({"role": "user"}) == []


@pytest.fixture
def summary_store(tmp_path):
    store = SessionStore(tmp_path / "summary.kohakutr")
    store.init_meta("summary", "agent", "config", "", ["a", "ab", "a:worker", "empty"])
    try:
        yield store
    finally:
        store.close(update_status=False)


def forbid_history_values(monkeypatch, store):
    tables = (store.events, store.channels, store.subagents)
    vault_type = type(store.events)

    def guard(original):
        def read(table, *args, **kwargs):
            if any(table is target for target in tables):
                pytest.fail("summary must not read history payloads")
            return original(table, *args, **kwargs)

        return read

    monkeypatch.setattr(vault_type, "__getitem__", guard(vault_type.__getitem__))
    monkeypatch.setattr(vault_type, "get", guard(vault_type.get))


class TestStoredSummaryCounts:
    def test_counts_keys_without_reading_values(
        self, summary_store, monkeypatch, capsys
    ):
        store = summary_store
        for key in (
            "a:e000000",
            "a:e000009",
            "a:e1000000",
            "ab:e000000",
            "a:worker:e000002",
        ):
            store.events[key] = b"unvalidated payload"
        for key in ("a:m000000", "a:m1000000", "ab:m000003", "a:worker:m000000"):
            store.channels[key] = b"unvalidated payload"
        for key in (
            "a:worker:explore:0:meta",
            "a:worker:explore:7:meta",
            "ab:plan:3:meta",
        ):
            store.subagents[key] = b"unvalidated payload"
        store.subagents["a:worker:explore:0:conversation"] = "not a run"
        forbid_history_values(monkeypatch, store)
        _inspect_session.print_summary(store)
        lines = capsys.readouterr().out.splitlines()
        assert lines[0:4] == [
            "Session: summary",
            "Type: agent",
            "Config: config",
            "Status: running",
        ]
        assert "Agents: ['a', 'ab', 'a:worker', 'empty']" in lines
        assert [line for line in lines if line.endswith(" events")] == [
            "  a: 3 events",
            "  ab: 1 events",
            "  a:worker: 1 events",
            "  empty: 0 events",
        ]
        assert "Channels: 3 (4 messages)" in lines
        assert "Sub-agent runs: 3" in lines
        assert "Counts describe stored records; payloads are not validated." in lines

    def test_invalid_record_keys_not_counted(self, summary_store, monkeypatch, capsys):
        store = summary_store
        for key in ("a:e", "a:e-1", "a:e12x", "a:e１２", "a:e000001:extra", "a:other"):
            store.events[key] = {}
        for key in ("a:m", "a:m-1", "a:m12x", "a:m１２", "a:m000001:extra", "a:other"):
            store.channels[key] = {}
        for key in (
            "a:meta",
            "a:worker:meta",
            "a:worker:-1:meta",
            "a:worker:１２:meta",
            "a:worker:0:conversation",
            "a:worker:0:meta:extra",
        ):
            store.subagents[key] = {}
        forbid_history_values(monkeypatch, store)
        _inspect_session.print_summary(store)
        out = capsys.readouterr().out
        assert "  a: 0 events" in out
        assert "Channels: 0 (0 messages)" in out
        assert "Sub-agent runs: 0" in out

    def test_counts_are_not_truncated_at_default_key_limit(
        self, summary_store, monkeypatch, capsys
    ):
        store = summary_store
        store.channels.enable_cache(flush_interval=60)
        store.subagents.enable_cache(flush_interval=60)
        for index in range(10005):
            store.events[f"a:e{index:06d}"] = {}
            store.channels[f"c{index}:m000000"] = {}
            store.subagents[f"a:explore:{index}:meta"] = {}
        store.flush()
        store.channels.flush_cache()
        store.subagents.flush_cache()
        forbid_history_values(monkeypatch, store)
        _inspect_session.print_summary(store)
        out = capsys.readouterr().out
        assert "  a: 10005 events" in out
        assert "Channels: 10005 (10005 messages)" in out
        assert "Sub-agent runs: 10005" in out

    def test_empty_tables_and_new_cached_events(
        self, summary_store, monkeypatch, capsys
    ):
        store = summary_store
        forbid_history_values(monkeypatch, store)
        _inspect_session.print_summary(store)
        first = capsys.readouterr().out
        assert "  a: 0 events" in first
        assert "Channels: 0 (0 messages)" in first
        assert "Sub-agent runs: 0" in first
        store.events["a:e000004"] = {}
        _inspect_session.print_summary(store)
        second = capsys.readouterr().out
        assert "  a: 1 events" in second

    @pytest.mark.parametrize("encode", [False, True])
    def test_key_parsing_handles_markers_in_names(self, encode):
        keys = [
            "a:e000000",
            "a:echild:e000001",
            "ab:e000005",
            "a:worker:e1000001",
            "a:echild:e000002",
        ]
        if encode:
            keys = [key.encode() for key in keys]
        assert _inspect_session._sequence_counts(keys, ":e") == {
            "a": 1,
            "a:echild": 2,
            "ab": 1,
            "a:worker": 1,
        }
        runs = [
            "a:explore:role:2:meta",
            "a:explore:role:2:conversation",
            "a:explore:1000000:meta",
        ]
        if encode:
            runs = [key.encode() for key in runs]
        assert _inspect_session._subagent_run_count(runs) == 2

    def test_undecodable_keys_are_excluded(self, summary_store, monkeypatch, capsys):
        store = summary_store
        for table in (store.events, store.channels, store.subagents):
            table[b"\xff"] = {}
        store.events["a:e000000"] = {}
        store.channels["tasks:m000000"] = {}
        store.subagents["a:explore:0:meta"] = {}
        forbid_history_values(monkeypatch, store)
        _inspect_session.print_summary(store)
        out = capsys.readouterr().out
        assert "  a: 1 events" in out
        assert "Channels: 1 (1 messages)" in out
        assert "Sub-agent runs: 1" in out

    def test_main_summary_preserves_legacy_lifecycle(
        self, summary_store, monkeypatch, capsys
    ):
        store = summary_store
        path = store.path
        store.append_event("a", "text", {"content": "summary only"})
        store.meta["status"] = "completed"
        store.close(update_status=False)
        opened = []

        def open_store(path):
            owned = SessionStore(path)
            opened.append(owned)
            forbid_history_values(monkeypatch, owned)
            return owned

        monkeypatch.setattr(_inspect_session, "SessionStore", open_store)
        monkeypatch.setattr(_inspect_session.sys, "argv", [str(SCRIPT_PATH), path])
        _inspect_session.main()
        out = capsys.readouterr().out
        assert "Status: completed" in out
        assert "  a: 1 events" in out
        assert "TEXT: summary only" not in out
        assert "Use --events" in out
        assert len(opened) == 1 and opened[0]._closed
        check = SessionStore.open_readonly(path)
        try:
            assert check.meta["status"] == "paused"
        finally:
            check.close()

    def test_explicit_detail_readers_still_show_payloads(self, summary_store, capsys):
        store = summary_store
        store.append_event("a", "text", {"content": "event detail"})
        store.save_channel_message(
            "team:tasks", {"sender": "a", "content": "channel detail"}
        )
        store.save_subagent(
            "a",
            "explore",
            0,
            {"task": "subagent detail", "turns": 2, "tools_used": [], "success": True},
            "[]",
        )
        _inspect_session.print_events(store, "a")
        _inspect_session.print_channels(store)
        _inspect_session.print_subagents(store)
        out = capsys.readouterr().out
        assert "TEXT: event detail" in out
        assert "team:tasks (1 messages):" in out
        assert "[a] channel detail" in out
        assert "task=subagent detail" in out
        assert "(conversation saved)" in out
