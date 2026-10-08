"""Idle-branch continuation: identity, admission, and persist/resume."""

import asyncio
from types import SimpleNamespace

import pytest

from kohakuterrarium import Terrarium
from kohakuterrarium.core.branch_continuation import (
    continue_on_branch,
    load_persisted_branch_view,
    persist_branch_view,
    selected_branch_view,
)
from kohakuterrarium.core.config_types import AgentConfig
from kohakuterrarium.errors import AgentNotRunningError, ConflictError
from kohakuterrarium.session.raw_history import append_user_event_pair
from kohakuterrarium.session.resume import resume_agent
from kohakuterrarium.testing.llm import ScriptedLLM


def _non_system(agent):
    return [
        (message.role, message.content)
        for message in agent.controller.conversation.get_messages()
        if message.role != "system"
    ]


def _non_system_prefix(messages):
    return [
        (item.get("role"), item.get("content"))
        for item in messages
        if item.get("role") != "system"
    ]


class _BarrierLLM(ScriptedLLM):
    def __init__(self, script, barrier_on_call: int | None = None):
        super().__init__(script)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.barrier_on_call = barrier_on_call
        self._calls_started = 0

    async def chat(self, messages, **kwargs):
        self._calls_started += 1
        if self.barrier_on_call == self._calls_started:
            self.release.clear()
            self.entered.set()
            await self.release.wait()
        async for chunk in super().chat(messages, **kwargs):
            yield chunk


async def _started(tmp_path, script, *, barrier_on_call=None):
    provider = (
        _BarrierLLM(script, barrier_on_call=barrier_on_call)
        if barrier_on_call is not None
        else ScriptedLLM(script)
    )
    engine = Terrarium(session_dir=str(tmp_path / "sessions"))
    await engine.__aenter__()
    creature = await engine.add_creature(
        AgentConfig(name="alice", system_prompt="plain"),
        llm=provider,
        io="headless",
        start=True,
    )
    return engine, creature, creature.agent, provider


def _snapshot(agent, provider):
    return {
        "conv": _non_system(agent),
        "view": selected_branch_view(agent),
        "persisted": load_persisted_branch_view(agent.session_store, "alice"),
        "turn": agent._turn_index,
        "branch": agent._branch_id,
        "path": list(agent._parent_branch_path),
        "calls": provider.call_count,
        "inbox": len(agent._event_inbox),
    }


async def test_no_store_rejects_a_different_branch():
    processed = []

    async def process(event):
        processed.append(event)
        return True

    agent = SimpleNamespace(
        session_store=None,
        config=SimpleNamespace(name="alice"),
        _parent_branch_path=[],
        _turn_index=1,
        _branch_id=1,
        _message_mutation_lock=None,
        _running=True,
        _paused=False,
        _process_event=process,
        _ensure_history_mutation_idle=lambda: None,
    )
    with pytest.raises(ConflictError, match="without a session store"):
        await continue_on_branch(agent, object(), {1: 2})
    assert processed == []


async def test_no_store_same_view_uses_baseline_input():
    processed = []

    async def process(event):
        processed.append(event.content)
        return True

    agent = SimpleNamespace(
        session_store=None,
        config=SimpleNamespace(name="alice"),
        _parent_branch_path=[],
        _turn_index=1,
        _branch_id=1,
        _message_mutation_lock=None,
        _running=True,
        _paused=False,
        _process_event=process,
        _ensure_history_mutation_idle=lambda: None,
    )
    event = SimpleNamespace(content="hello")
    assert await continue_on_branch(agent, event, {1: 1}) is True
    assert processed == ["hello"]


async def test_stopped_rejection_is_readonly(tmp_path):
    engine, creature, agent, provider = await _started(tmp_path, ["A1", "A1-rerun"])
    try:
        await creature.run("U1", timeout=8)
        await agent.regenerate_last_response()
        before = _snapshot(agent, provider)
        await agent.stop()
        with pytest.raises(AgentNotRunningError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        after = _snapshot(agent, provider)
        after.pop("inbox")
        before.pop("inbox")
        assert after == before
    finally:
        await engine.__aexit__(None, None, None)


async def test_same_branch_paused_input_runs_after_resume(tmp_path):
    engine, creature, agent, provider = await _started(tmp_path, ["A1", "A2", "queued"])
    try:
        await creature.run("U1", timeout=8)
        await creature.run("U2", timeout=8)
        agent.pause()
        queued = await agent.inject_input("queued-same", branch_view={1: 1, 2: 1})
        assert queued is False
        assert provider.call_count == 2
        agent.resume()
        for _ in range(80):
            if provider.call_count > 2:
                break
            await asyncio.sleep(0.02)
        assert provider.last_user_message == "queued-same"
        assert ("assistant", "queued") in _non_system(agent)
    finally:
        await engine.__aexit__(None, None, None)


async def test_continuation_during_rerun_write_is_rejected(tmp_path):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A1-rerun", "must-not-run"]
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = agent.session_store.run

    async def blocked_run(fn, *args, **kwargs):
        if fn is append_user_event_pair:
            entered.set()
            await release.wait()
        return await original(fn, *args, **kwargs)

    try:
        await creature.run("U1", timeout=8)
        agent.session_store.run = blocked_run
        rerun = asyncio.create_task(agent.regenerate_last_response())
        await asyncio.wait_for(entered.wait(), 2)
        calls = provider.call_count
        with pytest.raises(ConflictError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        with pytest.raises(ConflictError):
            await agent.inject_input("same-view", branch_view={1: 1})
        assert provider.call_count == calls
        release.set()
        await rerun
        assert "A1-rerun" in _assistant_contents(agent)
    finally:
        release.set()
        await engine.__aexit__(None, None, None)


def _assistant_contents(agent):
    return [
        message.content
        for message in agent.controller.conversation.get_messages()
        if message.role == "assistant"
    ]


async def test_reload_read_failure_never_generates_wrong_branch(tmp_path, monkeypatch):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A1-rerun", "must-not-run"]
    )
    try:
        await creature.run("U1", timeout=8)
        await agent.regenerate_last_response()
        before = _snapshot(agent, provider)
        monkeypatch.setattr(
            agent.session_store,
            "get_events",
            lambda name: (_ for _ in ()).throw(OSError("history unavailable")),
        )
        with pytest.raises(OSError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        monkeypatch.undo()
        after = _snapshot(agent, provider)
        assert after["conv"] == before["conv"]
        assert after["view"] == before["view"]
        assert after["persisted"] == before["persisted"]
        assert after["calls"] == before["calls"]
        assert "A1-rerun" in _assistant_contents(agent)
    finally:
        await engine.__aexit__(None, None, None)


async def test_persist_failure_keeps_live_agent_on_current_branch(
    tmp_path, monkeypatch
):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A1-rerun", "orig-cont"]
    )
    try:
        await creature.run("U1", timeout=8)
        await agent.regenerate_last_response()
        before = _snapshot(agent, provider)

        def boom(*_args, **_kwargs):
            raise OSError("cannot persist branch view")

        monkeypatch.setattr(
            "kohakuterrarium.core.branch_continuation.persist_branch_view", boom
        )
        with pytest.raises(OSError):
            await agent.inject_input("continue-original", branch_view={1: 1})
        assert _snapshot(agent, provider)["conv"] == before["conv"]
        assert agent._branch_id == before["branch"]
        assert provider.call_count == before["calls"]
        monkeypatch.undo()
        await agent.inject_input("continue-original", branch_view={1: 1})
        assert _non_system_prefix(provider.call_log[-1]) == [
            ("user", "U1"),
            ("assistant", "A1"),
            ("user", "continue-original"),
        ]
    finally:
        await engine.__aexit__(None, None, None)


async def test_same_view_input_queues_during_rerun_generation(tmp_path):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A1-rerun", "follow"], barrier_on_call=2
    )
    try:
        await creature.run("U1", timeout=8)
        rerun = asyncio.create_task(agent.regenerate_last_response())
        await asyncio.wait_for(provider.entered.wait(), 2)
        assert provider._calls_started == 2
        live = selected_branch_view(agent)
        inject = asyncio.create_task(agent.inject_input("follow-up", branch_view=live))
        await asyncio.sleep(0)
        assert provider.call_count == 1
        provider.release.set()
        await rerun
        await inject
        assert _non_system_prefix(provider.call_log[-1])[-2:] == [
            ("assistant", "A1-rerun"),
            ("user", "follow-up"),
        ]
        assert "A1-rerun" in _assistant_contents(agent)
        assert "follow" in _assistant_contents(agent)
    finally:
        provider.release.set()
        await engine.__aexit__(None, None, None)


async def test_resume_uses_saved_original_branch_not_latest(tmp_path):
    engine, creature, agent, provider = await _started(
        tmp_path, ["A1", "A1-rerun", "after-resume"]
    )
    session_path = None
    try:
        await creature.run("U1", timeout=8)
        await agent.regenerate_last_response()
        persist_branch_view(agent.session_store, "alice", {1: 1})
        session_path = agent.session_store.path
        await agent.stop()
    finally:
        await engine.__aexit__(None, None, None)

    resumed, store = resume_agent(session_path, io_mode="plain", llm=provider)
    try:
        assert selected_branch_view(resumed)[1] == 1
        assert _non_system(resumed) == [("user", "U1"), ("assistant", "A1")]
        await resumed.start()
        await resumed.inject_input("after-resume", branch_view={1: 1})
        assert _non_system_prefix(provider.call_log[-1]) == [
            ("user", "U1"),
            ("assistant", "A1"),
            ("user", "after-resume"),
        ]
        assert "A1-rerun" not in [
            content for role, content in _non_system_prefix(provider.call_log[-1])
        ]
    finally:
        await resumed.stop()
        store.close()
