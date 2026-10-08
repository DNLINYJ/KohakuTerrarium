"""Pin the idle-branch continuation failure boundaries from review da90bd28."""

import pytest

from kohakuterrarium import Terrarium
from kohakuterrarium.core.branch_continuation import (
    load_persisted_branch_view,
    persist_branch_view,
    selected_branch_view,
)
from kohakuterrarium.core.config_types import AgentConfig
from kohakuterrarium.errors import AgentNotRunningError, ConflictError
from kohakuterrarium.session.raw_history import append_user_event_pair
from kohakuterrarium.session.resume import resume_agent
from kohakuterrarium.testing.llm import ScriptedLLM


def _user_meta(agent):
    return [
        (message.content, dict(message.metadata or {}))
        for message in agent.controller.conversation.get_messages()
        if message.role == "user"
    ]


async def _started(tmp_path, script):
    provider = ScriptedLLM(script)
    engine = Terrarium(session_dir=str(tmp_path / "sessions"))
    await engine.__aenter__()
    creature = await engine.add_creature(
        AgentConfig(name="alice", system_prompt="plain"),
        llm=provider,
        io="headless",
        start=True,
    )
    return engine, creature, creature.agent, provider


async def _open_turn1_sibling(agent, content="same"):
    await agent.session_store.run(
        append_user_event_pair,
        agent.session_store,
        agent.config.name,
        {"content": content},
        1,
        2,
        [],
    )
    persist_branch_view(agent.session_store, agent.config.name, {1: 2})
    agent._reload_conversation_under_branch_view({1: 2})


async def test_review_duplicate_text_user_identity(tmp_path):
    engine, creature, agent, _provider = await _started(tmp_path, ["A1", "A2"])
    try:
        await creature.run("same", timeout=8)
        await creature.run("same", timeout=8)
        await _open_turn1_sibling(agent)
        await agent.inject_input("continue-original", branch_view={1: 1})
        metas = _user_meta(agent)
        first, second = metas[0], metas[1]
        assert first[0] == "same"
        assert second[0] == "same"
        assert first[1]["event_id"] != second[1]["event_id"]
        assert (first[1]["turn_index"], first[1]["branch_id"]) == (1, 1)
        assert (second[1]["turn_index"], second[1]["branch_id"]) == (2, 1)
    finally:
        await engine.__aexit__(None, None, None)


async def test_review_stopped_rejection_is_readonly(tmp_path):
    engine, creature, agent, provider = await _started(tmp_path, ["A1", "A2"])
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        await _open_turn1_sibling(agent, content="U1")
        before_path = selected_branch_view(agent)
        before_persisted = load_persisted_branch_view(agent.session_store, "alice")
        before_conv = [
            (m.role, m.content)
            for m in agent.controller.conversation.get_messages()
            if m.role != "system"
        ]
        calls = provider.call_count
        await agent.stop()
        with pytest.raises(AgentNotRunningError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        assert selected_branch_view(agent) == before_path
        assert load_persisted_branch_view(agent.session_store, "alice") == before_persisted
        assert [
            (m.role, m.content)
            for m in agent.controller.conversation.get_messages()
            if m.role != "system"
        ] == before_conv
        assert provider.call_count == calls
    finally:
        await engine.__aexit__(None, None, None)


async def test_review_same_branch_paused_input_preserves_baseline_queueing(tmp_path):
    engine, creature, agent, provider = await _started(tmp_path, ["A1", "A2"])
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        agent.pause()
        queued = await agent.inject_input("queued-same", branch_view={1: 1, 2: 1})
        assert queued is False
        assert provider.call_count == 2
        assert len(agent._event_inbox) == 1
        assert agent._event_inbox._dq[0].event.content == "queued-same"
    finally:
        agent.resume()
        await engine.__aexit__(None, None, None)


async def test_review_continuation_during_rerun_write_is_rejected(tmp_path):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A2", "must-not-run"]
    )
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        await _open_turn1_sibling(agent, content="U1")
        lock = agent._get_message_mutation_lock()
        await lock.acquire()
        calls = provider.call_count
        try:
            with pytest.raises(ConflictError):
                await agent.inject_input("continue-original", branch_view={1: 1})
            assert provider.call_count == calls
            assert selected_branch_view(agent)[1] == 2
        finally:
            lock.release()
    finally:
        await engine.__aexit__(None, None, None)


async def test_review_reload_read_failure_never_generates_wrong_branch(tmp_path, monkeypatch):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A2", "must-not-run"]
    )
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        await _open_turn1_sibling(agent, content="U1")
        before = [
            (m.role, m.content)
            for m in agent.controller.conversation.get_messages()
            if m.role != "system"
        ]
        before_view = load_persisted_branch_view(agent.session_store, "alice")
        calls = provider.call_count
        original = agent.session_store.get_events

        def failing_get_events(name):
            raise OSError("history unavailable")

        monkeypatch.setattr(agent.session_store, "get_events", failing_get_events)
        with pytest.raises(OSError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        monkeypatch.setattr(agent.session_store, "get_events", original)
        assert provider.call_count == calls
        assert load_persisted_branch_view(agent.session_store, "alice") == before_view
        assert [
            (m.role, m.content)
            for m in agent.controller.conversation.get_messages()
            if m.role != "system"
        ] == before
    finally:
        await engine.__aexit__(None, None, None)


async def test_review_persist_failure_does_not_silently_lose_selection(tmp_path, monkeypatch):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A2", "orig-reply"]
    )
    session_path = None
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        await _open_turn1_sibling(agent, content="U1")
        calls = provider.call_count
        def boom(*_args, **_kwargs):
            raise OSError("cannot persist branch view")

        monkeypatch.setattr(
            "kohakuterrarium.core.branch_continuation.persist_branch_view", boom
        )
        with pytest.raises(OSError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        assert provider.call_count == calls
        session_path = agent.session_store.path
    finally:
        await engine.__aexit__(None, None, None)

    resumed, store = resume_agent(session_path, io_mode="plain")
    try:
        assert selected_branch_view(resumed)[1] == 2
        contents = [
            m.content
            for m in resumed.controller.conversation.get_messages()
            if m.role != "system"
        ]
        assert "continue-original" not in contents
    finally:
        store.close()
