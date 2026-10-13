"""Unit tests for :mod:`kohakuterrarium.studio.persistence.viewer.summary`."""

import weakref
from collections import Counter

import pytest
from kohakuterrarium.errors import NotFoundError

from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.studio.persistence.viewer import rollups as rollups_mod
from kohakuterrarium.studio.persistence.viewer import summary as summary_mod
from kohakuterrarium.studio.persistence.viewer.summary import (
    _agents_for_summary,
    _aggregate_rollups,
    _scan_events_for_summary,
    _subagent_failed,
    build_summary_payload,
)

# ── helpers ──────────────────────────────────────────────────────


class TestSubagentFailed:
    def test_success_false(self):
        assert _subagent_failed({"type": "subagent_result", "success": False})

    def test_clean(self):
        assert not _subagent_failed({"type": "subagent_result", "success": True})

    def test_wrong_type(self):
        assert not _subagent_failed({"type": "tool_call"})


# ── _aggregate_rollups ──────────────────────────────────────────


class TestAggregateRollups:
    def test_basic(self):
        rows = [
            {"turn_index": 1, "tokens_in": 5, "tokens_out": 3, "tokens_cached": 1},
            {"turn_index": 2, "tokens_in": 2, "tokens_out": 1, "cost_usd": 0.01},
        ]
        out = _aggregate_rollups(rows)
        assert out["tokens"]["prompt"] == 7
        assert out["tokens"]["completion"] == 4
        assert out["cost_usd"] == 0.01

    def test_dedupe_turn_index(self):
        # Same turn appearing twice (e.g., one main + one attached)
        # counts as ONE turn for the totals.
        rows = [
            {"turn_index": 1, "tokens_in": 5, "tokens_out": 3},
            {"turn_index": 1, "tokens_in": 2, "tokens_out": 1},
        ]
        out = _aggregate_rollups(rows)
        assert out["turns"] == 1

    def test_count_by_agent_with_breakdown(self):
        rows = [
            {
                "turn_index": 1,
                "tokens_in": 0,
                "tokens_out": 0,
                "breakdown": [
                    {"kind": "main"},
                    {"kind": "subagent"},
                    {"kind": "main"},
                ],
            }
        ]
        out = _aggregate_rollups(rows, count_by_agent=True)
        # Subagent breakdown entry doesn't count.
        assert out["turns"] == 2

    def test_count_by_agent_no_breakdown(self):
        rows = [{"turn_index": 1}]
        out = _aggregate_rollups(rows, count_by_agent=True)
        # No breakdown → counted as 1.
        assert out["turns"] == 1

    def test_no_cost_returns_none(self):
        rows = [{"turn_index": 1, "tokens_in": 5}]
        out = _aggregate_rollups(rows)
        assert out["cost_usd"] is None

    def test_invalid_cost_skipped(self):
        rows = [{"turn_index": 1, "cost_usd": "bad"}]
        out = _aggregate_rollups(rows)
        assert out["cost_usd"] is None


# ── _scan_events_for_summary ───────────────────────────────────


class TestScanEvents:
    def test_counts_tool_calls(self):
        events = [
            {"type": "tool_call", "turn_index": 1},
            {"type": "tool_call", "turn_index": 1},
        ]
        out = _scan_events_for_summary(events)
        assert out["tool_calls"] == 2

    def test_tracks_error_turns(self):
        events = [
            {"type": "tool_error", "turn_index": 1},
            {"type": "tool_error", "turn_index": 1},  # same turn — dedup
            {"type": "subagent_error", "turn_index": 2},
        ]
        out = _scan_events_for_summary(events)
        assert sorted(out["error_turns"]) == [1, 2]

    def test_failed_tool_result_counts_as_error(self):
        events = [
            {"type": "tool_result", "turn_index": 3, "error": "boom"},
            {"type": "tool_result", "turn_index": 4, "exit_code": 2},
        ]
        out = _scan_events_for_summary(events)
        assert out["error_turns"] == [3, 4]

    def test_tracks_compact_turns(self):
        events = [
            {"type": "compact_complete", "turn_index": 1},
            {"type": "compact_replace", "turn_index": 2},
        ]
        out = _scan_events_for_summary(events)
        assert sorted(out["compact_turns"]) == [1, 2]

    def test_subagent_failed_counts_as_error(self):
        events = [
            {
                "type": "subagent_result",
                "turn_index": 1,
                "success": False,
            }
        ]
        out = _scan_events_for_summary(events)
        assert 1 in out["error_turns"]

    def test_spawned_in_turn_fallback(self):
        events = [{"type": "tool_error", "spawned_in_turn": 5}]
        out = _scan_events_for_summary(events)
        assert 5 in out["error_turns"]


# ── _agents_for_summary ────────────────────────────────────────


def _store(tmp_path, name="s.kohakutr") -> SessionStore:
    return SessionStore(str(tmp_path / name))


class TestAgentsForSummary:
    def test_default_returns_main_agents(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice", "bob"])
            meta = s.load_meta()
            out = _agents_for_summary(meta, s, None)
            assert "alice" in out
            assert "bob" in out
        finally:
            s.close()

    def test_explicit_known(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice"])
            meta = s.load_meta()
            assert _agents_for_summary(meta, s, "alice") == ["alice"]
        finally:
            s.close()

    def test_explicit_unknown_raises(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice"])
            meta = s.load_meta()
            with pytest.raises(NotFoundError):
                _agents_for_summary(meta, s, "ghost")
        finally:
            s.close()

    def test_event_only_creature_appears_in_agent_list(self, tmp_path):
        # A creature that wrote events but was never added to meta["agents"]
        # is counted in the graph token total; it must also appear in the
        # agent list. load_meta() folds discover_agents_from_events into
        # meta["agents"], so both sources agree — this pins that guarantee.
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "terrarium", "/p", "/w", ["root"])
            s.append_event(
                "worker",
                "turn_token_usage",
                {"prompt_tokens": 5, "completion_tokens": 2},
                turn_index=1,
            )
            s.flush()
            meta = s.load_meta()
            out = _agents_for_summary(meta, s, None)
            assert "root" in out
            assert "worker" in out
        finally:
            s.close()

    def test_viewer_default_at_front(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice"])
            s.append_event("alice:attached:rev:0", "x", {})
            s.set_viewer_default_agent("alice:attached:rev:0")
            s.flush()
            meta = s.load_meta()
            out = _agents_for_summary(meta, s, None)
            assert out[0] == "alice:attached:rev:0"
        finally:
            s.close()


# ── build_summary_payload ──────────────────────────────────────


class TestBuildSummaryPayload:
    def test_basic_session(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("my-sess", "agent", "/p", "/w", ["alice"])
            s.append_event(
                "alice",
                "turn_token_usage",
                {"prompt_tokens": 5, "completion_tokens": 3},
                turn_index=1,
            )
            s.append_event("alice", "tool_call", {"name": "bash"}, turn_index=1)
            s.append_event("alice", "tool_error", {"name": "bash"}, turn_index=1)
            s.flush()
            out = build_summary_payload(s, "my-sess", None)
            assert out["session_name"] == "my-sess"
            assert "alice" in out["agents"]
            assert out["totals"]["tool_calls"] == 1
            assert out["totals"]["errors"] == 1
        finally:
            s.close()

    def test_specific_agent(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice", "bob"])
            s.append_event(
                "alice",
                "turn_token_usage",
                {"prompt_tokens": 10},
                turn_index=1,
            )
            s.flush()
            out = build_summary_payload(s, "sess", "alice")
            assert out["agents"] == ["alice"]
        finally:
            s.close()

    def test_unknown_agent_raises(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice"])
            with pytest.raises(NotFoundError):
                build_summary_payload(s, "sess", "ghost")
        finally:
            s.close()

    def test_multi_creature_total_includes_turnless_creature(self, tmp_path):
        # UXI-03: a channel-driven creature never bumps turn_index, so its
        # usage is turn-less and the turn-bucketed aggregate drops it —
        # the graph TOTAL would then count only the (user-driven) root.
        # The total must sum EVERY creature.
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "terrarium", "/p", "/w", ["root", "worker"])
            s.append_event(
                "root",
                "turn_token_usage",
                {"prompt_tokens": 100, "completion_tokens": 40},
                turn_index=1,
            )
            # worker: no turn_index (channel-driven) — per-call token_usage.
            s.append_event(
                "worker",
                "token_usage",
                {"prompt_tokens": 50, "completion_tokens": 20},
            )
            s.flush()
            out = build_summary_payload(s, "sess", None)
            assert out["totals"]["tokens"]["prompt"] == 150
            assert out["totals"]["tokens"]["completion"] == 60
        finally:
            s.close()

    def test_straddling_subagent_job_not_double_counted_in_total(self, tmp_path):
        # UXI-03: a sub-agent job with a turn-ful snapshot AND a turn-less
        # result must be counted ONCE in the graph total — the turn-bucketed
        # aggregate and the turn-less fold used to each count it.
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "terrarium", "/p", "/w", ["root"])
            s.append_event(
                "root",
                "turn_token_usage",
                {"prompt_tokens": 100, "completion_tokens": 40},
                turn_index=1,
            )
            s.append_event(
                "root",
                "subagent_token_usage",
                {
                    "job_id": "j1",
                    "name": "explore",
                    "prompt_tokens": 30,
                    "completion_tokens": 20,
                    "total_tokens": 50,
                },
                turn_index=1,
            )
            # Final result recorded turn-less (job straddles the boundary).
            s.append_event(
                "root",
                "subagent_result",
                {
                    "job_id": "j1",
                    "name": "explore",
                    "prompt_tokens": 48,
                    "completion_tokens": 32,
                    "total_tokens": 80,
                },
            )
            s.flush()
            out = build_summary_payload(s, "sess", None)
            # parent 100/40 + sub-agent ONCE at its max 48/32 = 148/72
            # (NOT 100/40 + 30/20 + 48/32 = 178/92).
            assert out["totals"]["tokens"]["prompt"] == 148
            assert out["totals"]["tokens"]["completion"] == 72
        finally:
            s.close()

    def test_multi_creature_turned_total_not_double_counted(self, tmp_path):
        # Regression: when both creatures DO have turn_index, the turn-less
        # fold-in adds nothing — the total must not double count.
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "terrarium", "/p", "/w", ["root", "worker"])
            s.append_event(
                "root",
                "turn_token_usage",
                {"prompt_tokens": 100, "completion_tokens": 40},
                turn_index=1,
            )
            s.append_event(
                "worker",
                "turn_token_usage",
                {"prompt_tokens": 50, "completion_tokens": 20},
                turn_index=1,
            )
            s.flush()
            out = build_summary_payload(s, "sess", None)
            assert out["totals"]["tokens"]["prompt"] == 150
            assert out["totals"]["tokens"]["completion"] == 60
        finally:
            s.close()

    def test_includes_hot_turns(self, tmp_path):
        s = _store(tmp_path)
        try:
            s.init_meta("sess", "agent", "/p", "/w", ["alice"])
            for i in range(1, 7):
                s.append_event(
                    "alice",
                    "turn_token_usage",
                    {"prompt_tokens": i * 10},
                    turn_index=i,
                )
            s.flush()
            out = build_summary_payload(s, "sess", "alice")
            hot = out["hot_turns"]
            # Top-5 turns ranked by token usage, heaviest first — turn 6
            # (60 tokens) leads, turn 1 (10 tokens) is dropped.
            assert [t["turn_index"] for t in hot] == [6, 5, 4, 3, 2]
            assert hot[0]["tokens_in"] == 60
        finally:
            s.close()


@pytest.fixture
def overview_store(tmp_path):
    store = _store(tmp_path)
    store.init_meta("synthetic", "terrarium", "", "", ["alice", "empty"])
    store.meta["viewer_default_agent"] = "bob"
    store.meta["forked_children"] = ["child"]
    for _ in range(2):
        store.append_event("alice", "tool_call", {"name": "read"}, turn_index=1)
    for _ in range(2):
        store.append_event(
            "alice",
            "token_usage",
            {"prompt_tokens": 100, "completion_tokens": 20},
            turn_index=1,
        )
    store.append_event(
        "alice",
        "turn_token_usage",
        {"prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 2},
        turn_index=1,
    )
    store.append_event(
        "alice", "token_usage", {"prompt_tokens": 7, "completion_tokens": 2}
    )
    store.append_event(
        "alice",
        "subagent_token_usage",
        {
            "job_id": "job1",
            "name": "explore",
            "prompt_tokens": 5,
            "completion_tokens": 1,
            "cost_usd": 0.1,
        },
        turn_index=1,
    )
    store.append_event(
        "alice",
        "subagent_token_usage",
        {
            "job_id": "job1",
            "name": "explore",
            "prompt_tokens": 8,
            "completion_tokens": 2,
        },
        turn_index=2,
    )
    store.append_event(
        "alice",
        "subagent_result",
        {
            "job_id": "job1",
            "name": "explore",
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "cost_usd": 0.2,
            "success": True,
        },
        turn_index=2,
    )
    store.append_event(
        "alice",
        "subagent_result",
        {"prompt_tokens": 2, "completion_tokens": 1, "success": False},
        turn_index=3,
    )
    store.append_event("alice", "compact_complete", {}, turn_index=2)
    store.append_event("alice", "tool_result", {"exit_code": 1}, turn_index=1)
    store.save_turn_rollup(
        "alice",
        1,
        {
            "tokens_in": 50,
            "tokens_out": 6,
            "tokens_cached": 4,
            "cost_usd": 0.4,
            "tool_calls": 0,
        },
    )
    store.save_turn_rollup(
        "alice", 4, {"tokens_in": 2, "tokens_out": 1, "cost_usd": None}
    )
    store.append_event(
        "bob",
        "turn_token_usage",
        {"prompt_tokens": 4, "completion_tokens": 1},
        turn_index=1,
    )
    store.append_event(
        "alice:attached:review:0",
        "token_usage",
        {"prompt_tokens": 3, "completion_tokens": 2},
        turn_index=1,
    )
    store.flush()
    try:
        yield store
    finally:
        store.close(update_status=False)


def _count_summary_reads(monkeypatch, store):
    reads, values, dedupes = Counter(), Counter(), []
    original_read = SessionStore.get_events
    original_dedupe = rollups_mod.dedupe_adjacent_duplicate_events
    vault_type = type(store.events)

    def read(owner, name, *args, **kwargs):
        if owner is store:
            reads[name] += 1
        return original_read(owner, name, *args, **kwargs)

    def value_reader(original):
        def value(vault, key, *args, **kwargs):
            if vault is store.events:
                text = key.decode() if isinstance(key, bytes) else key
                values[text.rsplit(":e", 1)[0]] += 1
            return original(vault, key, *args, **kwargs)

        return value

    def dedupe(events):
        dedupes.append(len(events))
        return original_dedupe(events)

    monkeypatch.setattr(SessionStore, "get_events", read)
    monkeypatch.setattr(vault_type, "__getitem__", value_reader(vault_type.__getitem__))
    monkeypatch.setattr(vault_type, "get", value_reader(vault_type.get))
    monkeypatch.setattr(rollups_mod, "dedupe_adjacent_duplicate_events", dedupe)
    monkeypatch.setattr(
        summary_mod, "dedupe_adjacent_duplicate_events", dedupe, raising=False
    )
    return reads, values, dedupes


class TestSummaryReadReuse:
    @pytest.mark.parametrize(
        "agent", [None, "alice", "empty", "bob", "alice:attached:review:0"]
    )
    def test_one_read_and_dedupe_per_namespace(
        self, overview_store, monkeypatch, agent
    ):
        store = overview_store
        names = (
            [name for name, _ in rollups_mod.list_agent_namespaces(store)]
            if agent is None
            else [agent]
        )
        event_counts = {name: len(store.get_events(name)) for name in names}
        reads, values, dedupes = _count_summary_reads(monkeypatch, store)
        payload = build_summary_payload(store, "sample", agent)
        assert reads == Counter({name: 1 for name in names})
        assert values == Counter(event_counts)
        assert len(dedupes) == len(names)
        assert set(payload["agents"]) == set(names)
        assert (
            payload["totals"]["tokens"]["prompt"]
            == {
                None: 35,
                "alice": 63,
                "empty": 0,
                "bob": 4,
                "alice:attached:review:0": 3,
            }[agent]
        )

    @pytest.mark.parametrize("agent", [None, "alice"])
    def test_complete_summary_contract(self, overview_store, agent):
        store = overview_store
        meta = store.load_meta()
        graph = agent is None
        expected = {
            key: meta.get(key)
            for key in (
                "format_version",
                "status",
                "created_at",
                "last_active",
                "config_type",
                "config_path",
            )
        }
        expected.update(
            session_name="sample",
            session_id="synthetic",
            lineage={},
            agents=(
                ["bob", "alice", "empty", "alice:attached:review:0"]
                if graph
                else ["alice"]
            ),
            totals={
                "turns": 6 if graph else 4,
                "tokens": {
                    "prompt": 35 if graph else 63,
                    "completion": 12 if graph else 11,
                    "cached": 2 if graph else 4,
                },
                "cost_usd": 0.6000000000000001,
                "tool_calls": 2,
                "errors": 2,
                "compacts": 1,
                "forks": 1,
                "attached_agents": 1,
            },
            error_turns=[1, 3],
            compact_turns=[2],
            hot_turns=[
                {
                    "agent": agent,
                    "turn_index": turn,
                    "cost_usd": cost,
                    "tokens_in": tin,
                    "tokens_out": tout,
                }
                for turn, cost, tin, tout in [
                    (3, None, 2, 1),
                    (4, None, 2, 1),
                    (1, 0.4, 57 if graph else 50, 9 if graph else 6),
                    (2, 0.2, 9, 3),
                ]
            ],
        )
        assert build_summary_payload(store, "sample", agent) == expected

    @pytest.mark.parametrize("name", ["", 7, [], {}])
    def test_diagnostic_only_metadata_namespace(self, tmp_path, monkeypatch, name):
        store = _store(tmp_path)
        try:
            store.init_meta("odd", "agent", "", "", [name])
            store.append_event(str(name), "tool_call", {"name": "read"}, turn_index=1)
            store.append_event(str(name), "tool_result", {"exit_code": 1}, turn_index=1)
            reads, _, _ = _count_summary_reads(monkeypatch, store)
            result = build_summary_payload(store, "sample", None)
            assert result["agents"][0] == name
            assert result["totals"]["tool_calls"] == len(result["agents"])
            assert result["error_turns"] == [1]
            assert reads == Counter({str(agent): 1 for agent in result["agents"]})
        finally:
            store.close()

    @pytest.mark.parametrize("name", ["", [], {}])
    def test_empty_metadata_namespace_without_events(self, tmp_path, name):
        store = _store(tmp_path)
        try:
            store.init_meta("odd", "agent", "", "", [name])
            result = build_summary_payload(store, "sample", None)
            assert result["agents"] == [name]
            assert result["totals"]["tool_calls"] == 0
            assert result["totals"]["turns"] == 0
        finally:
            store.close()

    def test_payload_counter_detects_hidden_scan(self, overview_store, monkeypatch):
        store = overview_store
        names = [name for name, _ in rollups_mod.list_agent_namespaces(store)]
        budget = sum(len(store.get_events(name)) for name in names)
        _, values, _ = _count_summary_reads(monkeypatch, store)
        original_scan = summary_mod._scan_events_for_summary

        def hidden_scan(events):
            next(store.events.values())
            return original_scan(events)

        monkeypatch.setattr(summary_mod, "_scan_events_for_summary", hidden_scan)
        build_summary_payload(store, "sample", None)
        with pytest.raises(AssertionError):
            assert sum(values.values()) == budget
        assert sum(values.values()) > budget

    def test_append_after_read_is_visible_only_on_next_request(
        self, overview_store, monkeypatch
    ):
        store = overview_store
        expected = build_summary_payload(store, "sample", None)
        original = SessionStore.get_events
        appended = False

        def read(owner, name, *args, **kwargs):
            nonlocal appended
            events = original(owner, name, *args, **kwargs)
            if owner is store and name == "alice" and not appended:
                appended = True
                store.append_event("alice", "tool_call", {"name": "late"}, turn_index=1)
                store.append_event("alice", "token_usage", {"prompt_tokens": 11})
            return events

        monkeypatch.setattr(SessionStore, "get_events", read)
        assert build_summary_payload(store, "sample", None) == expected
        later = build_summary_payload(store, "sample", None)
        assert later["totals"]["tool_calls"] == expected["totals"]["tool_calls"] + 1
        assert (
            later["totals"]["tokens"]["prompt"]
            == expected["totals"]["tokens"]["prompt"] + 11
        )

    def test_previous_namespace_payloads_are_released(
        self, overview_store, monkeypatch
    ):
        store = overview_store
        original = SessionStore.get_events
        previous = []

        class Event(dict):
            pass

        def read(owner, name, *args, **kwargs):
            if owner is not store:
                return original(owner, name, *args, **kwargs)
            assert all(ref() is None for ref in previous)
            previous.clear()
            events = [Event(event) for event in original(owner, name, *args, **kwargs)]
            previous.extend(weakref.ref(event) for event in events)
            return events

        monkeypatch.setattr(SessionStore, "get_events", read)
        result = build_summary_payload(store, "sample", None)
        assert all(ref() is None for ref in previous)
        assert result["totals"]["tool_calls"] == 2
