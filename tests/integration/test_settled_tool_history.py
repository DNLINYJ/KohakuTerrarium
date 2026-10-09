"""Settled tool history under branch continuation and resume.

One native workflow: grouped direct calls, a late background result, sibling
continuation, and two cold resumes. The LLM is scripted; tools and session
store are real.
"""

import asyncio

from kohakuterrarium import Terrarium
from kohakuterrarium.core.branch_continuation import live_branch_path
from kohakuterrarium.core.config_types import AgentConfig
from kohakuterrarium.llm.base import NativeToolCall
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.session.resume import resume_agent
from kohakuterrarium.testing.llm import ScriptedLLM


class _Echo(BaseTool):
    def __init__(self):
        super().__init__()
        self.calls = 0

    @property
    def tool_name(self):
        return "echo"

    @property
    def description(self):
        return "Echo."

    @property
    def execution_mode(self):
        return ExecutionMode.DIRECT

    async def _execute(self, args, **kwargs):
        self.calls += 1
        return ToolResult(output=f"echoed:{args.get('text', '')}")


class _SlowBg(BaseTool):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def tool_name(self):
        return "slowbg"

    @property
    def description(self):
        return "Background tool."

    @property
    def execution_mode(self):
        return ExecutionMode.BACKGROUND

    async def _execute(self, args, **kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ToolResult(output="RESULT_reviewbg")


class _PlanLLM(ScriptedLLM):
    def __init__(self, plan):
        super().__init__(["x"] * max(len(plan), 1))
        self.plan = plan
        self._last_tool_calls = []

    @property
    def last_tool_calls(self):
        return self._last_tool_calls

    async def chat(self, messages, **kwargs):
        normalized = self._normalize_messages(messages)
        self.call_log.append(normalized)
        idx = min(self.call_count, len(self.plan) - 1)
        step = self.plan[idx]
        self.call_count += 1
        self._last_tool_calls = []
        if isinstance(step, NativeToolCall):
            self._last_tool_calls = [step]
            yield ""
            return
        if isinstance(step, list):
            self._last_tool_calls = step
            yield ""
            return
        yield step


def _prefix(messages):
    rows = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            rows.append(
                ("assistant-calls", [call.get("id") for call in message["tool_calls"]])
            )
        elif role == "tool":
            rows.append(("tool", message.get("content")))
        else:
            rows.append((role, message.get("content")))
    return rows


async def _wait_result(agent, output="RESULT_reviewbg"):
    for _ in range(80):
        events = agent.session_store.get_events(agent.config.name)
        if any(
            event.get("type") == "tool_result" and event.get("output") == output
            for event in events
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("settled tool result never recorded")


async def test_settled_tools_survive_sibling_continuation_and_resume(tmp_path):
    echo = _Echo()
    bg = _SlowBg()
    llm = _PlanLLM(
        [
            [
                NativeToolCall("c1", "echo", '{"text":"a"}'),
                NativeToolCall("c2", "echo", '{"text":"b"}'),
            ],
            NativeToolCall("b1", "slowbg", "{}"),
            "second-turn",
            "t2-sibling",
            "orig-cont",
            "sib-cont",
            "after-resume",
            "after-second-resume",
        ]
    )
    session_path = None
    async with Terrarium(session_dir=str(tmp_path / "s")) as engine:
        creature = await engine.add_creature(
            AgentConfig(name="alice", system_prompt="plain", tool_format="native"),
            llm=llm,
            tools=[echo, bg],
            io="headless",
            start=True,
        )
        agent = creature.agent
        await creature.run("do two", timeout=12)
        grouped = _prefix(llm.call_log[1])
        assert ("assistant-calls", ["c1", "c2"]) in grouped
        assert ("tool", "echoed:a") in grouped
        assert ("tool", "echoed:b") in grouped
        first = asyncio.create_task(creature.run("start bg", timeout=20))
        await asyncio.wait_for(bg.started.wait(), 5)
        await first
        await creature.run("second", timeout=12)
        bg.release.set()
        await _wait_result(agent)
        original = dict(live_branch_path(agent))
        await agent.regenerate_last_response()
        sibling = dict(live_branch_path(agent))
        assert sibling != original
        await agent.inject_input("continue original", branch_view=original)
        orig = _prefix(llm.call_log[-1])
        await agent.inject_input("continue sibling", branch_view=sibling)
        sib = _prefix(llm.call_log[-1])
        for prefix in (orig, sib):
            assert ("assistant-calls", ["c1", "c2"]) in prefix
            assert ("tool", "echoed:a") in prefix
            assert ("tool", "echoed:b") in prefix
            assert ("assistant-calls", ["b1"]) in prefix
            assert ("tool", "RESULT_reviewbg") in prefix
            assert prefix.count(("tool", "RESULT_reviewbg")) == 1
        assert ("user", "continue original") in orig
        assert ("user", "continue sibling") in sib
        assert echo.calls == 2
        assert bg.calls == 1
        session_path = agent.session_store.path
        await agent.stop()

    resumed, store = resume_agent(session_path, io_mode="plain", llm=llm)
    try:
        await resumed.start()
        await resumed.inject_input("after-resume")
        after = _prefix(llm.call_log[-1])
        assert ("tool", "RESULT_reviewbg") in after
        assert ("assistant-calls", ["b1"]) in after
        assert after[-1] == ("user", "after-resume")
        session_path = resumed.session_store.path
        await resumed.stop()
    finally:
        store.close()

    resumed2, store2 = resume_agent(session_path, io_mode="plain", llm=llm)
    try:
        await resumed2.start()
        await resumed2.inject_input("after-second-resume")
        after2 = _prefix(llm.call_log[-1])
        assert after2.count(("tool", "RESULT_reviewbg")) == 1
        assert after2[-1] == ("user", "after-second-resume")
        assert echo.calls == 2
        assert bg.calls == 1
    finally:
        await resumed2.stop()
        store2.close()
