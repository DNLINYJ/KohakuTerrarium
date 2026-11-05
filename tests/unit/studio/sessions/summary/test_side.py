"""Side titles reuse the summary pipeline without inherited context or stale writes."""

import asyncio
import json

import pytest

from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.studio.identity.session_summary import SummarySettings
from kohakuterrarium.studio.sessions.summary.side import side_messages, summarize_side
from kohakuterrarium.testing.llm import ScriptedLLM

MESSAGES = [
    {"role": "user", "content": "PARENT SECRET: migrate the database"},
    {"role": "assistant", "content": "PARENT SECRET: schema migration"},
    {
        "role": "user",
        "content": "[Private context fork instructions]\nPRIVATE instructions",
    },
    {"role": "user", "content": "How do I bake sourdough?"},
    {"role": "assistant", "content": "Use an active starter."},
]
EVENT = {
    "job_id": "side-job",
    "name": "fork",
    "run": 0,
    "audience": "user",
    "side_turn": 1,
}


class GatedLLM(ScriptedLLM):
    def __init__(self, script):
        super().__init__(script)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        async for chunk in super().chat(*args, **kwargs):
            yield chunk


@pytest.fixture
def store(tmp_path):
    store = SessionStore(str(tmp_path / "side.kohakutr"))
    store.init_meta("side", "agent", "", str(tmp_path), ["main"])
    store.save_subagent(
        "main",
        "fork",
        0,
        {
            **EVENT,
            "title": "How do I bake sourdough?",
            "status": "done",
            "inherited_message_count": 2,
            "total_tokens": 80,
        },
        json.dumps({"messages": MESSAGES}),
    )
    yield store
    store.close(update_status=False)


async def refresh(store, llm=None, settings=None, event=None):
    return await summarize_side(
        store,
        settings or SummarySettings("llm", 2, "title-model"),
        parent="main",
        event=event or EVENT,
        llm_factory=lambda: llm,
    )


def update(store, **changes):
    meta = store.load_subagent_meta("main", "fork", 0)
    store.save_subagent("main", "fork", 0, {**meta, **changes})


async def test_llm_reads_only_side_and_changes_only_title_fields(store):
    llm = ScriptedLLM(['"Baking sourdough with a starter."\nExtra text'])
    before = store.load_subagent_meta("main", "fork", 0)
    transcript = store.load_subagent_conversation("main", "fork", 0)
    parent = store.load_meta()
    result = await refresh(store, llm)
    assert result == {
        **before,
        "title": "Baking sourdough with a starter",
        "title_source": "llm",
        "title_at_turn": 1,
    }
    assert "sourdough" in llm.last_user_message
    assert "active starter" in llm.last_user_message
    assert "PARENT SECRET" not in str(llm.call_log)
    assert "PRIVATE instructions" not in str(llm.call_log)
    assert "Private context fork instructions" not in str(llm.call_log)
    assert store.load_meta() == parent
    assert store.load_subagent_conversation("main", "fork", 0) == transcript


@pytest.mark.parametrize("source", ["off", "heuristic", "compaction"])
async def test_non_llm_sources_do_not_construct_provider(store, source):
    def forbidden():
        pytest.fail("provider must not be constructed")

    result = await summarize_side(
        store,
        SummarySettings(source, 2, "ignored"),
        parent="main",
        event=EVENT,
        llm_factory=forbidden,
    )
    meta = store.load_subagent_meta("main", "fork", 0)
    assert meta["title"] == "How do I bake sourdough?"
    if source == "off":
        assert result is None
        assert "title_at_turn" not in meta
    else:
        assert meta["title_source"] == "heuristic"
        assert meta["title_at_turn"] == 1


async def test_cadence_duplicate_results_and_fallback_do_not_downgrade(store):
    llm = ScriptedLLM(["Sourdough starter", "Bread fermentation"])
    assert (await refresh(store, llm))["title"] == "Sourdough starter"
    assert await refresh(store, llm) is None
    update(store, side_turn=2)
    assert await refresh(store, llm, event={**EVENT, "side_turn": 2}) is None
    update(store, side_turn=3)
    result = await refresh(store, llm, event={**EVENT, "side_turn": 3})
    assert result["title"] == "Bread fermentation"
    assert result["title_at_turn"] == 3
    assert llm.call_count == 2
    update(store, side_turn=5)
    assert await refresh(store, event={**EVENT, "side_turn": 5}) is None
    assert store.load_subagent_meta("main", "fork", 0)["title"] == "Bread fermentation"
    result = await refresh(
        store,
        settings=SummarySettings("heuristic", 1),
        event={**EVENT, "side_turn": 5},
    )
    assert result["title_source"] == "heuristic"


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": "other-job"},
        {"audience": "controller"},
        {"status": "running"},
        {"side_turn": 2},
        {"title_source": "user", "title": "My title"},
        {"inherited_message_count": None},
        {"inherited_message_count": -1},
        {"inherited_message_count": 999},
    ],
)
async def test_invalid_or_stale_run_never_calls_model(store, changes):
    update(store, **changes)
    before = store.load_subagent_meta("main", "fork", 0)
    llm = ScriptedLLM(["Wrong title"])
    assert await refresh(store, llm) is None
    assert llm.call_count == 0
    assert store.load_subagent_meta("main", "fork", 0) == before


@pytest.mark.parametrize(
    "change",
    ["followup", "running", "transcript", "boundary", "user_title", "identity"],
)
async def test_slow_summary_cannot_overwrite_a_changed_run(store, change):
    llm = GatedLLM(["Stale title"])
    task = asyncio.create_task(refresh(store, llm))
    await asyncio.wait_for(llm.started.wait(), 2)
    match change:
        case "followup":
            update(store, side_turn=2, title="Followup title")
        case "running":
            update(store, status="running")
        case "transcript":
            meta = store.load_subagent_meta("main", "fork", 0)
            store.save_subagent("main", "fork", 0, meta, '{"messages": []}')
        case "boundary":
            update(store, inherited_message_count=3)
        case "user_title":
            update(store, title="Chosen title", title_source="user")
        case "identity":
            update(store, job_id="other-job")
    before = store.load_subagent_meta("main", "fork", 0)
    transcript = store.load_subagent_conversation("main", "fork", 0)
    llm.release.set()
    assert await task is None
    assert store.load_subagent_meta("main", "fork", 0) == before
    assert store.load_subagent_conversation("main", "fork", 0) == transcript


async def test_slow_summary_preserves_newer_usage_metadata(store):
    llm = GatedLLM(["Sourdough starter"])
    task = asyncio.create_task(refresh(store, llm))
    await asyncio.wait_for(llm.started.wait(), 2)
    update(store, total_tokens=900, new_field="retained")
    llm.release.set()
    result = await task
    assert result["title"] == "Sourdough starter"
    assert result["total_tokens"] == 900
    assert result["new_field"] == "retained"


@pytest.mark.parametrize("boundary", [None, -1, "2", True, 100])
def test_invalid_boundaries_fail_closed(boundary):
    assert side_messages(json.dumps({"messages": MESSAGES}), boundary) == []


def test_filter_multimodal_instruction_and_slice_before_other_filtering():
    messages = [
        {"role": "tool", "content": "inherited tool"},
        {"role": "user", "content": "inherited question"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "  [Private context fork instructions]"}
            ],
        },
        {"role": "user", "content": "side question"},
    ]
    assert side_messages(json.dumps({"messages": messages}), 2) == [messages[-1]]


async def test_missing_meta_or_transcript_does_not_generate_title(store):
    llm = ScriptedLLM(["Wrong title"])
    assert await refresh(store, llm, event={**EVENT, "run": 10}) is None
    store.save_subagent("main", "fork", 10, {**EVENT, "run": 10})
    assert await refresh(store, llm, event={**EVENT, "run": 10}) is None
    assert llm.call_count == 0
