"""Repair command argument and context validation."""

from kohakuterrarium.builtins.user_commands import get_builtin_user_command
from kohakuterrarium.modules.user_command.base import UserCommandContext


async def test_repair_command_rejects_missing_context_and_invalid_arguments():
    command = get_builtin_user_command("repair-images")
    assert (
        await command.execute("", UserCommandContext())
    ).error == "No agent context."
    for args in ("--force", "--apply", "--apply one two"):
        result = await command.execute(args, UserCommandContext(agent=object()))
        assert result.error.startswith("Usage: /repair-images")
