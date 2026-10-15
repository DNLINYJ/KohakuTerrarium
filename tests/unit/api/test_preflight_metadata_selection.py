"""Request-local metadata selection for the local preflight suffix only."""

from collections import Counter
from contextlib import closing
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import kohakuterrarium.api.routes.persistence.resume as route
import kohakuterrarium.api.routes.persistence.resume_cluster as cluster
import kohakuterrarium.session.version as version
from kohakuterrarium.api.app import create_app
from kohakuterrarium.api.deps import get_service_factory
from kohakuterrarium.api.routes.persistence.preflight_metadata import (
    local_metadata_reader,
)
from kohakuterrarium.core.config import AgentConfig
from kohakuterrarium.core.config_serde import pack_agent_config
from kohakuterrarium.session.metadata_candidate import (
    MetadataReadError,
    read_metadata_candidate,
)
from kohakuterrarium.session.readonly import read_session_meta
from kohakuterrarium.session.resource_gate import ResourceReleaseError
from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.terrarium.graph_manifest import MANIFEST_KEY


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setenv("KT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KT_SESSION_DIR", str(root))
    path = root / "saved.kohakutr"
    with closing(SessionStore(path)) as store:
        store.init_meta("saved", "agent", "", str(tmp_path), ["alice"])
    counts = Counter()

    def copied(path, label):
        counts[label] += 1
        return read_session_meta(path)

    def selected(path):
        result = read_metadata_candidate(path, eligible=True)
        counts[result.reason] += 1
        return result.meta

    monkeypatch.setattr(
        cluster, "read_session_meta", partial(copied, label="classification")
    )
    monkeypatch.setattr(version, "read_session_meta", partial(copied, label="version"))
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield app, client, path, counts, selected
    finally:
        client.close()


@pytest.mark.parametrize("manifest", [False, True])
def test_request_selection_defaults_legacy_and_does_not_leak(setup, manifest):
    app, client, path, counts, selected = setup
    if manifest:
        with closing(SessionStore(path)) as store:
            store.meta[MANIFEST_KEY] = {
                "kind": "kohakuterrarium.live_graph",
                "version": 1,
                "revision": 1,
                "graph_id": "saved",
                "creatures": [
                    {
                        "creature_id": "alice",
                        "name": "alice",
                        "config_snapshot": pack_agent_config(AgentConfig(name="alice")),
                        "source_ref": None,
                        "pwd": str(path.parent),
                        "is_privileged": False,
                        "parent_creature_id": None,
                    }
                ],
                "channels": [],
                "listen": [],
                "send": [],
            }
    assert local_metadata_reader() is None
    baseline = client.post("/api/sessions/saved/resume/preflight")
    assert baseline.status_code == 200
    counts.clear()

    def select(request: Request):
        return selected if request.headers.get("x-test-candidate") == "1" else None

    app.dependency_overrides[local_metadata_reader] = select
    response = client.post(
        "/api/sessions/saved/resume/preflight", headers={"x-test-candidate": "1"}
    )
    assert response.status_code == 200
    assert response.json() == baseline.json()
    assert counts == {"classification": 2, "version": 1, "ok": 1 if manifest else 2}
    counts.clear()
    normal = client.post("/api/sessions/saved/resume/preflight")
    assert normal.json() == baseline.json()
    assert counts == {"classification": 2, "version": 1}
    counts.clear()
    replaced = client.post(
        "/api/sessions/saved/resume/preflight",
        json={"pwd": str(path.parent)},
        headers={"x-test-candidate": "1"},
    )
    assert replaced.status_code == 200
    assert replaced.json()["ready"] is True
    assert counts == {"classification": 2, "version": 1, "ok": 2}


@pytest.mark.parametrize("fail_on", [1, 2])
@pytest.mark.parametrize(
    "stage,status,code",
    [
        ("resource", 503, "session_metadata_resource_unavailable"),
        ("sql", 500, "session_metadata_read_failed"),
        ("decode", 500, "session_metadata_read_failed"),
        ("gate", 503, "session_metadata_resource_unavailable"),
    ],
)
def test_failure_is_structured_and_never_ready(setup, stage, status, code, fail_on):
    app, client, path, counts, _ = setup

    def fail(path):
        counts["attempts"] += 1
        if counts["attempts"] < fail_on:
            return read_session_meta(path)
        if stage == "gate":
            raise ResourceReleaseError("secret filesystem path")
        raise MetadataReadError(stage, OSError("secret payload"))

    app.dependency_overrides[local_metadata_reader] = lambda: fail
    result = client.post("/api/sessions/saved/resume/preflight")
    assert result.status_code == status
    assert result.json()["detail"]["code"] == code
    assert "secret" not in result.text and "ready" not in result.json()
    assert counts == {"classification": 2, "version": 1, "attempts": fail_on}


def test_remote_and_cluster_never_invoke_local_reader(setup, monkeypatch):
    app, client, path, counts, _ = setup

    def fail(path):
        raise AssertionError("local metadata reader reached from remote branch")

    app.dependency_overrides[local_metadata_reader] = lambda: fail
    app.dependency_overrides[get_service_factory] = lambda: lambda: SimpleNamespace(
        host=object()
    )
    calls = []

    async def remote(host, path, node, **kwargs):
        calls.append(node)
        return {"legacy": True, "ready": True, "members": [], "gaps": []}

    monkeypatch.setattr(route, "_worker_workspace_preflight", remote)
    result = client.post(
        "/api/sessions/saved/resume/preflight", json={"on_node": "worker"}
    )
    assert result.status_code == 200 and result.json()["ready"]
    assert calls == ["worker"]
    assert counts == {"classification": 2}
    counts.clear()
    calls.clear()
    result = client.post(
        "/api/sessions/saved/resume/preflight",
        json={"members": [{"sid": "saved", "on_node": "worker"}]},
    )
    assert result.status_code == 200 and result.json()["ready"], result.text
    assert calls == ["worker"]
    assert counts == {"classification": 2}
