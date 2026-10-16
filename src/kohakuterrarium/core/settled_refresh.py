"""Restore completed background results before truncating stable history."""

from kohakuterrarium.core.agent_message_history import (
    apply_prepared_branch_view,
    reload_conversation_under_branch_view,
    snapshot_needs_settled_tool_replay,
)
from kohakuterrarium.core.branch_continuation import live_branch_path
from kohakuterrarium.core.branch_conversation import build_branch_conversation
from kohakuterrarium.errors import ConflictError


def refresh_mutation_prefix(agent, index: int) -> int:
    """Refresh a pending prefix from saved results and return its user target index."""
    conversation = agent.controller.conversation
    target = conversation.get_messages()[index]
    if target.role != "user":
        return index
    prefix = conversation.to_messages(include_metadata=True)[:index]
    if agent.session_store is None or not any(
        message.get("metadata", {}).get("tool_result_phase") == "pending"
        for message in prefix
    ):
        return index
    events = agent.session_store.get_events(agent.config.name)
    view = live_branch_path(agent)
    if not snapshot_needs_settled_tool_replay(events, view, prefix):
        return index
    turn = target.metadata.get("turn_index")
    branch = target.metadata.get("branch_id")
    if not isinstance(turn, int) or not isinstance(branch, int):
        raise ConflictError("Cannot identify pending-history mutation target")
    prepared = reload_conversation_under_branch_view(
        agent, view, events=events, apply=False
    )
    matches = [
        i
        for i, message in enumerate(prepared["messages"])
        if message.get("role") == "user"
        and message.get("metadata", {}).get("turn_index") == turn
        and message.get("metadata", {}).get("branch_id") == branch
    ]
    if len(matches) != 1:
        raise ConflictError("Cannot restore pending-history mutation target")
    prepared = {**prepared, "messages": prepared["messages"][: matches[0] + 1]}
    candidate = build_branch_conversation(conversation, prepared["messages"])
    if not any(
        message.role == "user"
        and message.metadata.get("turn_index") == turn
        and message.metadata.get("branch_id") == branch
        for message in candidate.get_messages()
    ):
        raise ConflictError("History retention would remove the mutation target")
    apply_prepared_branch_view(agent, prepared, candidate=candidate)
    return next(
        i
        for i, message in enumerate(conversation.get_messages())
        if message.role == "user"
        and message.metadata.get("turn_index") == turn
        and message.metadata.get("branch_id") == branch
    )
