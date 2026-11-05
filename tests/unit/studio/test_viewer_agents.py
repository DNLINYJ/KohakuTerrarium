"""Shared viewer namespace enumeration and default ordering."""

import pytest

from kohakuterrarium.errors import NotFoundError
from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.studio.persistence.viewer.agents import viewer_agent_names


@pytest.mark.parametrize(
    "default", [None, "", "unknown", 17, "attached:attached:review:0"]
)
def test_default_order_and_requested_namespace(tmp_path, default):
    store = SessionStore(tmp_path / "agents.kohakutr")
    attached = "attached:attached:review:0"
    try:
        store.init_meta("s", "agent", "/p", "/w", ["alice", "bob"])
        store.append_event(attached, "user_input", {"content": "review"})
        meta = store.load_meta()
        meta["viewer_default_agent"] = default
        expected = ["alice", "bob", attached]
        if default == attached:
            expected = [attached, "alice", "bob"]
        assert viewer_agent_names(meta, store) == expected
        assert viewer_agent_names(meta, store, attached) == [attached]
        assert viewer_agent_names(meta, store, "bob") == ["bob"]
        with pytest.raises(NotFoundError, match="Agent not found"):
            viewer_agent_names(meta, store, "missing")
    finally:
        store.close(update_status=False)


def test_empty_and_duplicate_attached_namespace(tmp_path):
    store = SessionStore(tmp_path / "empty.kohakutr")
    try:
        assert viewer_agent_names(store.load_meta(), store) == []
        attached = "alice:attached:review:0"
        store.init_meta("s", "agent", "/p", "/w", ["alice", attached])
        store.append_event(attached, "user_input", {"content": "review"})
        assert viewer_agent_names(store.load_meta(), store).count(attached) == 1
    finally:
        store.close(update_status=False)
