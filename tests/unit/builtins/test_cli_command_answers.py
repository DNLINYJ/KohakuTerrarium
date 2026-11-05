"""CLI command answers share the foreground stdin reader."""

import asyncio
import queue

import pytest

from kohakuterrarium.builtins.inputs.cli import CLIInput
from kohakuterrarium.modules.user_command.base import UserCommandResult


@pytest.mark.parametrize(
    "kind,answer", [("confirm", "n"), ("select", ""), ("confirm", "/btw question")]
)
async def test_command_answer_uses_existing_reader(monkeypatch, kind, answer):
    lines = queue.Queue()
    module = CLIInput()
    module._running = True
    module.command_idle_waiter = lambda: asyncio.sleep(0)
    reads = []

    def read_line():
        reads.append(True)
        return lines.get(timeout=2)

    def competing_input(*args):
        pytest.fail("command opened a second stdin consumer")

    monkeypatch.setattr(module, "_read_line", read_line)
    monkeypatch.setattr("builtins.input", competing_input)
    reader = asyncio.create_task(module.get_input())
    data = {"type": kind, "options": [{"value": "a", "label": "A"}]}
    prompt = asyncio.create_task(
        module.render_command_data(UserCommandResult(data=data), kind)
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert not prompt.done()
        lines.put(answer)
        result = await asyncio.wait_for(prompt, 1)
        assert result.output == "Cancelled."
        lines.put("ordinary")
        assert (await asyncio.wait_for(reader, 1)).content == "ordinary"
        assert len(reads) == 2
    finally:
        lines.put(None)
        reader.cancel()
        prompt.cancel()
        await asyncio.gather(reader, prompt, return_exceptions=True)
    assert module._command_answer is None


async def test_queued_prompt_after_eof_returns_cancelled_answer():
    module = CLIInput()
    module._running = True
    module._exit_requested = True
    module.command_idle_waiter = lambda: asyncio.sleep(0)
    assert await asyncio.wait_for(module._read_command_answer("answer: "), 0.1) == ""
    assert module._command_answer is None


@pytest.mark.parametrize("stop", [True, False])
async def test_stop_or_cancel_clears_command_answer(stop):
    module = CLIInput()
    module._running = True
    module.command_idle_waiter = lambda: asyncio.sleep(0)
    prompt = asyncio.create_task(module._read_command_answer("answer: "))
    await asyncio.sleep(0)
    if stop:
        await module.stop()
    else:
        prompt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prompt
    assert module._command_answer is None
