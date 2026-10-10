"""Branch-matching helpers for resume (kept small for the file-size guard).

Split out of :mod:`resume` so it stays under the line limit. These helpers
decide whether a saved conversation snapshot still matches the branch the
agent resumes onto, or must be rebuilt from the event log.
"""

import re
from typing import Any

from kohakuterrarium.core.agent_message_history import (
    include_settled_tool_results,
    replay_with_settled_tools,
)
from kohakuterrarium.core.branch_continuation import load_persisted_branch_view
from kohakuterrarium.core.conversation_elide import ELISION_MARKER, TOOL_FEEDBACK_KIND
from kohakuterrarium.core.tool_output import render_content_text
from kohakuterrarium.session.history import (
    compact_covered_event_ids,
    select_live_event_ids,
)


def snapshot_has_turn_metadata(snapshot: list[dict]) -> bool:
    """Return whether the snapshot carries valid user turn metadata.

    Edit/regenerate targeting resolves the edited turn through message
    metadata; legacy snapshots saved without it force content matching,
    which is ambiguous when a turn's wording repeats. Such snapshots are
    backfilled from the event log so targeting stays deterministic.

    A metadata entry is only "valid" when ``turn_index`` and ``branch_id``
    are positive ints — the exact shape every writer produces (backfill,
    replay, live snapshot). A non-int (corrupt/legacy-typed) value would be
    trusted here but fail edit targeting, so it must trigger backfill too.
    A non-dict entry is malformed data that ``_build_conversation`` would
    crash on, so it must also trigger backfill/replay rather than being
    trusted.
    """
    return all(
        isinstance(m, dict)
        and (
            m.get("role") != "user"
            or (
                isinstance(m.get("metadata"), dict)
                and m["metadata"].get("kind") == TOOL_FEEDBACK_KIND
            )
            or (
                isinstance(m.get("metadata"), dict)
                and isinstance(m["metadata"].get("turn_index"), int)
                and m["metadata"]["turn_index"] > 0
                and isinstance(m["metadata"].get("branch_id"), int)
                and m["metadata"]["branch_id"] > 0
            )
        )
        for m in snapshot
    )


def backfill_turn_metadata(snapshot: list[dict], events: list[dict]) -> list[dict]:
    """Backfill user-turn metadata onto a legacy metadata-less snapshot.

    The snapshot is the canonical persisted state (compaction exists only
    there), so it is trusted verbatim: a full replay would resurrect the
    pre-compact history and drop snapshot-only in-flight messages. Turn
    identity is recovered from the live ``user_message`` events so edit
    targeting stays deterministic.

    Snapshot user messages hold the most recent turns verbatim (compaction
    summarizes the prefix and keeps only the live zone), so they map to the
    LAST ``user_message`` events, not the first ones.
    """
    live_ids = set(select_live_event_ids(events))
    meta_by_pos: list[dict] = []
    pos_by_key: dict[tuple[int, int], int] = {}
    type_by_key: dict[tuple[int, int], str] = {}
    for evt in events:
        if evt.get("type") not in ("user_message", "user_input"):
            continue
        if isinstance(evt.get("event_id"), int) and evt["event_id"] not in live_ids:
            continue
        ti = evt.get("turn_index")
        bi = evt.get("branch_id")
        if not isinstance(ti, int) or not isinstance(bi, int):
            continue
        key = (ti, bi)
        etype = evt.get("type", "")
        meta = {
            # Only a user_message carries an editable event_id; a legacy
            # user_input's id is NOT valid for edit targeting
            # (select_raw_history_prefix rejects non-user_message targets),
            # so leave it unset when the chosen source is user_input.
            "event_id": evt.get("event_id") if etype == "user_message" else None,
            "turn_index": ti,
            "branch_id": bi,
        }
        if key in pos_by_key:
            # A turn usually carries BOTH user_input (written first) and
            # user_message (the canonical editable event). Edit targeting
            # requires event_id to point at a user_message, so if we recorded
            # a user_input first, replace it with the user_message event_id.
            # A duplicate user_message must NOT overwrite the first one.
            if etype == "user_message" and type_by_key[key] != "user_message":
                meta_by_pos[pos_by_key[key]] = meta
                type_by_key[key] = etype
            continue
        type_by_key[key] = etype
        pos_by_key[key] = len(meta_by_pos)
        meta_by_pos.append(meta)
    user_messages = [
        m
        for m in snapshot
        if isinstance(m, dict)
        and m.get("role") == "user"
        and not (
            isinstance(m.get("metadata"), dict)
            and m["metadata"].get("kind") == TOOL_FEEDBACK_KIND
        )
    ]
    tail_meta = meta_by_pos[-len(user_messages) :] if user_messages else []
    out: list[dict] = []
    for msg in snapshot:
        if not isinstance(msg, dict):
            # Malformed snapshot entry (corrupt data): keep it verbatim so
            # backfill never crashes here; resume's downstream build drops or
            # tolerates it rather than trusting a broken message shape.
            out.append(msg)
            continue
        m = dict(msg)
        raw_metadata = m.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        is_canonical_user = (
            m.get("role") == "user" and metadata.get("kind") != TOOL_FEEDBACK_KIND
        )
        if is_canonical_user and tail_meta:
            meta = dict(metadata)
            # Position-aligned: every user message consumes one tail slot so
            # later messages keep their correct turn. Replace the canonical
            # identity fields whenever turn or branch is not a positive int
            # (missing, corrupt, or legacy-typed) — a partial/valid-looking
            # value must not survive and later fail edit targeting.
            ti = meta.get("turn_index")
            bi = meta.get("branch_id")
            if not (isinstance(ti, int) and ti > 0 and isinstance(bi, int) and bi > 0):
                meta["turn_index"] = tail_meta[0]["turn_index"]
                meta["branch_id"] = tail_meta[0]["branch_id"]
                if tail_meta[0].get("event_id") is not None:
                    meta["event_id"] = tail_meta[0]["event_id"]
                else:
                    meta.pop("event_id", None)
                m["metadata"] = meta
            tail_meta = tail_meta[1:]
        out.append(m)
    return out


def is_path_prefix(sub: list[tuple[int, int]], full: list[tuple[int, int]]) -> bool:
    """Whether ``sub`` is a strict/equal prefix of ``full``."""
    return len(sub) <= len(full) and full[: len(sub)] == sub


def _safe_branch_path(raw: Any) -> list[tuple[int, int]]:
    """Parse a persisted branch path defensively.

    Malformed entries (int, string, wrong length, non-int coords) are
    skipped so resume never crashes on corrupt state.
    """
    out: list[tuple[int, int]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            t, b = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            continue
        out.append((t, b))
    return out


def snapshot_mismatches_branch(store: Any, agent: Any, agent_name: str) -> bool:
    """Whether the saved snapshot belongs to a different branch than the one
    resume lands on (the latest live subtree, restored by
    ``_restore_turn_branch_state``).

    A snapshot tagged with a branch that is an ANCESTOR of the target branch
    is still usable — resume appends the post-snapshot tail. Only a snapshot
    whose path diverges (a sibling branch) must be discarded and rebuilt.
    """
    try:
        branch = store.state.get(f"{agent_name}:snapshot_branch")
    except (KeyError, TypeError):
        return False
    if not isinstance(branch, dict):
        return False  # legacy snapshot without a tag -> trust it
    ti = branch.get("turn_index")
    bi = branch.get("branch_id")
    if not isinstance(ti, int) or not isinstance(bi, int) or ti <= 0 or bi <= 0:
        return False
    a_ti = getattr(agent, "_turn_index", None)
    a_bi = getattr(agent, "_branch_id", None)
    a_ppath = getattr(agent, "_parent_branch_path", None) or []
    # An agent with no live selection (core code can set turn/branch to 0) has
    # no branch to compare against — treat it as "no mismatch" and trust the
    # snapshot rather than triggering an unnecessary replay.
    if not isinstance(a_ti, int) or not isinstance(a_bi, int) or a_ti <= 0 or a_bi <= 0:
        return False
    snapshot_path = _safe_branch_path(branch.get("parent_branch_path")) + [(ti, bi)]
    agent_path = _safe_branch_path(a_ppath) + [(a_ti, a_bi)]
    return not is_path_prefix(snapshot_path, agent_path)


def snapshot_needs_settled_tool_replay(
    events: list[dict],
    branch_view: dict[int, int] | None,
    snapshot: list[dict] | None = None,
) -> bool:
    """Whether the snapshot still lacks a completed result for a live call."""
    if not events:
        return False
    live_ids = select_live_event_ids(events, branch_view=branch_view)
    projected = include_settled_tool_results(
        events,
        live_ids,
        covered_event_ids=compact_covered_event_ids(events, branch_view),
    )
    needed: dict[str, set[str]] = {}
    provider_ids: dict[str, str] = {}
    alias_owners: dict[str, set[str]] = {}
    for event in projected:
        if event.get("type") != "tool_call":
            continue
        job_id = str(event.get("call_id") or event.get("job_id") or "")
        provider_id = str(event.get("tool_call_id") or "")
        if job_id:
            alias_owners.setdefault(job_id, set()).add(job_id)
            if provider_id:
                provider_ids[job_id] = provider_id
                alias_owners.setdefault(provider_id, set()).add(job_id)
    for event in projected:
        if not event.get("_settled_from_call"):
            continue
        job_id = str(event.get("call_id") or event.get("job_id") or "")
        if not job_id:
            continue
        names = {
            job_id,
            provider_ids.get(job_id, ""),
            str(event.get("tool_call_id") or ""),
        }
        names.discard("")
        needed[job_id] = names
        for name in names:
            alias_owners.setdefault(name, set()).add(job_id)
    if not needed:
        return False
    if not snapshot:
        return True
    present: set[str] = set()
    for message in snapshot:
        identities = _snapshot_result_identities(message)
        if not identities:
            continue
        if not _snapshot_result_is_final(message):
            continue
        for identity in identities:
            owners = alias_owners.get(identity, set())
            if len(owners) == 1:
                present.update(owners & needed.keys())
    return any(job_id not in present for job_id in needed)


def _snapshot_result_identities(message: dict) -> set[str]:
    role = message.get("role")
    metadata = (
        message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    )
    names: set[str] = set()
    if role != "tool" and not (
        role == "user" and metadata.get("kind") == TOOL_FEEDBACK_KIND
    ):
        return names
    for key in ("job_id", "call_id"):
        if metadata.get(key):
            names.add(str(metadata[key]))
    job_ids = metadata.get("job_ids")
    if isinstance(job_ids, list):
        names.update(str(value) for value in job_ids if value)
    if names:
        return names
    if role == "tool":
        identity = str(message.get("tool_call_id") or "")
        return {identity} if identity else set()
    content = render_content_text(message.get("content") or "")
    names.update(
        re.findall(
            r"(?:\A|\n\n)\[Tool ([^\]\n]+?) "
            r"(?:completed|failed(?:, exit [^\]\n]+)?|cancelled)\](?=\n|\Z)",
            content,
        )
    )
    return names


def _snapshot_result_is_final(message: dict) -> bool:
    metadata = (
        message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    )
    phase = metadata.get("tool_result_phase")
    if phase == "pending":
        return False
    if phase == "final":
        return True
    content = render_content_text(message.get("content") or "")
    if re.match(
        r"^(?:\[[^\]\n]+\] )?(?:Running in background — task delegated\.|"
        r"Promoted to background\. Result arrives in a later turn\.)",
        content,
    ):
        return False
    if metadata.get("tool_results_elided") or ELISION_MARKER in content:
        return True
    if message.get("role") == "user" and metadata.get("kind") == TOOL_FEEDBACK_KIND:
        return bool(_snapshot_result_identities(message))
    return True


def replayed_messages_for(
    store: Any,
    agent_name: str,
    events: list[dict] | None = None,
) -> list[dict]:
    """Replay the latest live subtree from the event log (branch-aware).

    Used by resume when the saved snapshot belongs to a different branch.
    ``events`` is the caller's shared raw read; without it the store is
    read here.
    """
    if events is None:
        try:
            events = list(store.get_events(agent_name))
        except Exception:  # pragma: no cover - defensive
            return []
    if not events:
        return []
    persisted = load_persisted_branch_view(store, agent_name)
    return replay_with_settled_tools(
        events,
        branch_view=persisted,
        include_metadata=True,
        resumable=True,
    )
