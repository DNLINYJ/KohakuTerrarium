"""Unit tests for :mod:`kohakuterrarium.builtins.user_commands.clear`."""

from types import SimpleNamespace

from kohakuterrarium.builtins.user_commands.clear import ClearCommand
from kohakuterrarium.core.token_ledger import ledger_for
from kohakuterrarium.modules.user_command.base import UserCommandContext


class _Conversation:
    def __init__(self) -> None:
        self.messages = ["a", "b", "c"]

    def get_messages(self) -> list:
        return list(self.messages)


class _Controller:
    def __init__(self) -> None:
        self.conversation = _Conversation()

    def flush(self) -> None:
        self.conversation.messages = []


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def notify_activity(self, kind, detail, metadata=None) -> None:
        self.calls.append((kind, detail, metadata))


async def test_clear_zeroes_context_fill_and_reports_it():
    agent = SimpleNamespace(
        config=SimpleNamespace(name="alice"),
        controller=_Controller(),
        output_router=_Router(),
        session_store=None,
    )
    ledger_for(agent).add({"prompt_tokens": 5000, "completion_tokens": 50})
    result = await ClearCommand().execute("--force", UserCommandContext(agent=agent))
    assert result.error is None
    assert ledger_for(agent).context_tokens == 0
    assert ledger_for(agent).totals()["total"] == 5050
    kind, _, metadata = agent.output_router.calls[0]
    assert kind == "context_cleared"
    assert metadata == {"messages_cleared": 3, "context_tokens": 0}
