"""Ask a private side question without interrupting the main conversation."""

from kohakuterrarium.builtins.user_commands.registry import register_user_command
from kohakuterrarium.modules.user_command.base import (
    BaseUserCommand,
    CommandLayer,
    UserCommandContext,
    UserCommandResult,
)


@register_user_command("btw")
class BtwCommand(BaseUserCommand):
    name = "btw"
    description = "Ask a context-aware side question; --continue/--open/--stop JOB_ID"
    layer = CommandLayer.AGENT
    safe_while_busy = True

    async def _execute(
        self, args: str, context: UserCommandContext
    ) -> UserCommandResult:
        agent = context.agent
        if agent is None:
            return UserCommandResult(error="No creature selected")
        manager = agent.subagent_manager
        manager._check_side_enabled()
        text = args.strip()
        if not text:
            return UserCommandResult(
                output="Usage: /btw QUESTION | /btw --continue JOB_ID QUESTION | /btw --open JOB_ID | /btw --stop JOB_ID"
            )
        if text.startswith("--"):
            pieces = text.split(None, 2)
            if len(pieces) < 2 or pieces[0] not in {"--continue", "--open", "--stop"}:
                return UserCommandResult(
                    error="Use --continue, --open, or --stop with a side conversation job ID"
                )
            action, job_id = pieces[:2]
            conversation, meta = manager.side_conversation(job_id)
            if action == "--stop":
                stopped = await manager.cancel(job_id)
                if stopped:
                    meta = {**meta, "status": "cancelled"}
                return UserCommandResult(
                    output=(
                        "Side conversation stopped"
                        if stopped
                        else "Side conversation is not running"
                    ),
                    data={"type": "side_conversation", **meta},
                )
            if action == "--continue":
                if len(pieces) != 3 or not pieces[2].strip():
                    return UserCommandResult(error="A follow-up question is required")
                await manager.continue_side(job_id, pieces[2])
                _, meta = manager.side_conversation(job_id)
                output = f"Continuing side conversation {job_id}"
            else:
                messages = conversation.get_messages()[
                    meta.get("inherited_message_count", 0) :
                ]
                output = (
                    "\n\n".join(
                        f"{message.role}: {message.get_text_content()}"
                        for message in messages
                        if message.role in {"user", "assistant"}
                        and not message.get_text_content().startswith(
                            "[Private context fork instructions]"
                        )
                    )
                    or "Side conversation is starting"
                )
        else:
            job_id = await manager.start_side(text)
            _, meta = manager.side_conversation(job_id)
            output = (
                f"Side conversation {job_id} started. /btw --open {job_id} to view it."
            )
        return UserCommandResult(
            output=output, data={"type": "side_conversation", **meta}
        )
