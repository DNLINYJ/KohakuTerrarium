"""Raw persisted-history helpers for message branch mutations."""

from typing import Any, Protocol

from kohakuterrarium.core.agent_message_history import (
    replay_with_settled_tools,
    snapshot_needs_settled_tool_replay,
)
from kohakuterrarium.core.compact_branch import compact_rule_from_event
from kohakuterrarium.core.conversation import Conversation
from kohakuterrarium.core.snapshot_history import (
    prepare_snapshot,
    replayed_compact_state,
)
from kohakuterrarium.llm.message import dicts_to_messages
from kohakuterrarium.session.history import (
    compact_covered_event_ids,
    normalize_tool_call_events,
    replay_conversation,
)
from kohakuterrarium.session.raw_history import (
    UserMessageSelector,
    select_raw_history_prefix,
)


class RawHistoryAgent(Protocol):
    session_store: Any
    config: Any
    controller: Any
    _turn_index: int
    _branch_id: int
    _parent_branch_path: list[tuple[int, int]]


def raw_target_content(
    agent: RawHistoryAgent,
    target: UserMessageSelector,
    *,
    branch_view: dict[int, int] | None = None,
):
    """Read canonical target content without changing agent state."""
    if agent.session_store is None:
        raise ValueError("raw persisted history is unavailable")
    prefix = select_raw_history_prefix(
        agent.session_store.get_events(agent.config.name),
        selector=target,
        branch_view=branch_view,
    )
    return prefix.target.get("content", "")


def reload_raw_prefix_for_target(
    agent: RawHistoryAgent,
    target: UserMessageSelector,
    *,
    branch_view: dict[int, int] | None = None,
) -> None:
    """Restore the target prefix using only summaries with unchanged inputs."""
    if agent.session_store is None:
        raise ValueError("raw persisted history is unavailable")
    current_conversation = agent.controller.conversation
    all_events = list(agent.session_store.get_events(agent.config.name))
    prefix = select_raw_history_prefix(
        all_events,
        selector=target,
        branch_view=branch_view,
    )
    target_path: set[tuple[int, int]] = {
        (turn, branch)
        for turn, branch in prefix.branch_view.items()
        if turn < prefix.target.get("turn_index")
    }
    reusable_compacts = [
        evt
        for evt in all_events
        if (rule := compact_rule_from_event(evt)) is not None
        and (inputs := rule.inputs()) is not None
        and inputs.issubset(target_path)
    ]
    raw_events = [
        event
        for event in prefix.events
        if event.get("type")
        not in {
            "compact_replace",
            "compact_complete",
            "conversation_snapshot",
        }
    ]
    raw_events.extend(reusable_compacts)
    raw_events.append(prefix.target)
    # A covering compact can carry an event_id larger than the target's;
    # restore stream order so replay sees a monotonic event stream (its
    # pending-summary flush/drop logic assumes one).
    raw_events.sort(key=lambda evt: evt.get("event_id", 0))
    prepared = prepare_snapshot(
        agent.session_store,
        agent.config.name,
        all_events,
        prefix.branch_view,
        replay=replay_with_settled_tools,
        needs_replay=snapshot_needs_settled_tool_replay,
        before_turn=target.turn_index,
    )
    messages = (
        prepared["messages"]
        + replay_conversation([prefix.target], include_metadata=True)
        if prepared
        else replay_conversation(
            normalize_tool_call_events(
                raw_events,
                covered_event_ids=compact_covered_event_ids(
                    raw_events, prefix.branch_view
                ),
            ),
            branch_view=prefix.branch_view,
            include_metadata=True,
        )
    )
    persisted_messages = dicts_to_messages(messages)
    for raw_message, message in zip(messages, persisted_messages):
        metadata = raw_message.get("metadata")
        if isinstance(metadata, dict):
            message.metadata = dict(metadata)
    if not any(message.role == "system" for message in persisted_messages):
        persisted_messages = [
            *(
                message
                for message in agent.controller.conversation.get_messages()
                if message.role == "system"
            ),
            *persisted_messages,
        ]
    conversation = Conversation(current_conversation.config)
    for message in persisted_messages:
        conversation.append_message(message)
    agent.controller.conversation = conversation
    agent._context_cutoff_event_id = (
        prepared.get("context_cutoff_event_id", 0) if prepared is not None else 0
    )
    manager = getattr(agent, "compact_manager", None)
    if manager is not None:
        state = (
            prepared["compact_state"] if prepared else replayed_compact_state(messages)
        )
        manager._compact_count = state["compact_count"]
        manager._last_compact_time = state["last_compact_time"]
    agent._turn_index = target.turn_index
    agent._branch_id = target.branch_id
    agent._parent_branch_path = [
        (turn, branch)
        for turn, branch in prefix.branch_view.items()
        if 0 < turn < target.turn_index and branch > 0
    ]
