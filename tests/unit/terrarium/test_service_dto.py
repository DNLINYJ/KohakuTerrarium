"""Unit tests for :mod:`kohakuterrarium.terrarium.service_dto`."""

from types import SimpleNamespace

from kohakuterrarium.core.token_ledger import ledger_for
from kohakuterrarium.terrarium.service_dto import CreatureInfo, creature_to_info
from kohakuterrarium.testing.terrarium import _FakeAgent


def _creature(agent) -> SimpleNamespace:
    return SimpleNamespace(
        creature_id="cid",
        name="alice",
        graph_id="g",
        is_running=True,
        is_privileged=False,
        parent_creature_id=None,
        listen_channels=["a"],
        send_channels=[],
        agent=agent,
    )


class TestCreatureToInfoUsage:
    def test_snapshot_carries_running_usage(self):
        agent = _FakeAgent(name="alice")
        ledger_for(agent).add({"prompt_tokens": 120, "completion_tokens": 30})
        info = creature_to_info(_creature(agent))
        assert info.token_usage == {
            "prompt": 120,
            "completion": 30,
            "cached": 0,
            "total": 150,
        }
        assert info.context_tokens == 120

    def test_snapshot_without_agent_reports_zero(self):
        info = creature_to_info(_creature(None))
        assert info.token_usage["total"] == 0
        assert info.context_tokens == 0


class TestCreatureInfoIdentity:
    def test_usage_is_not_part_of_identity(self):
        base = dict(
            creature_id="cid",
            name="n",
            graph_id="g",
            is_running=True,
            is_privileged=False,
            parent_creature_id=None,
            listen_channels=(),
            send_channels=(),
        )
        busy = CreatureInfo(**base, token_usage={"total": 5}, context_tokens=5)
        assert busy == CreatureInfo(**base)
        assert hash(busy) == hash(CreatureInfo(**base))
