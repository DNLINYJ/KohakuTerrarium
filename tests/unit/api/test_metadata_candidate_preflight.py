"""Controlled local preflight candidate injection; production selection stays off."""

from collections import Counter
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import kohakuterrarium.api.routes.persistence.resume as route
import kohakuterrarium.api.routes.persistence.resume_cluster as cluster
import kohakuterrarium.session.metadata_candidate as candidate
import kohakuterrarium.session.version as version
import kohakuterrarium.terrarium.workspace_resume as workspace
from kohakuterrarium.api.app import create_app
from kohakuterrarium.api.deps import get_service_factory
from kohakuterrarium.core.config import AgentConfig
from kohakuterrarium.core.config_serde import pack_agent_config
from kohakuterrarium.session.readonly import read_session_meta
from kohakuterrarium.session.resource_gate import GATE
from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.terrarium.graph_manifest import MANIFEST_KEY


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("KT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KT_SESSION_DIR", str(sessions))
    path = sessions / "saved.kohakutr"
    with closing(SessionStore(path)) as store:
        store.init_meta("saved", "agent", "", str(tmp_path), ["alice"])
    app = create_app()
    counts = Counter()

    def service():
        counts["service"] += 1
        raise RuntimeError("no remote host in this controlled assembly")

    app.dependency_overrides[get_service_factory] = lambda: service

    def old_read(path, phase):
        counts[phase] += 1
        return read_session_meta(path)

    def selected(path):
        counts["selected"] += 1
        result = candidate.read_metadata_candidate(path, eligible=True)
        counts[result.reason] += 1
        return result.meta

    monkeypatch.setattr(
        cluster, "read_session_meta", lambda p: old_read(p, "classification")
    )
    monkeypatch.setattr(version, "read_session_meta", lambda p: old_read(p, "version"))
    monkeypatch.setattr(workspace, "read_session_meta", selected)
    monkeypatch.setattr(route, "read_session_meta", selected)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, path, counts
    finally:
        client.close()


@pytest.mark.parametrize("manifest", [False, True])
def test_local_suffix_keeps_classification_and_version_legacy(controlled, manifest):
    client, path, counts = controlled
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
    before = path.read_bytes()
    response = client.post("/api/sessions/saved/resume/preflight")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert counts == {
        "classification": 2,
        "version": 1,
        "selected": 1 if manifest else 2,
        "ok": 1 if manifest else 2,
    }
    assert path.read_bytes() == before
    assert not GATE.snapshot(path).registered


@pytest.mark.parametrize("fault", ["source", "decoder"])
def test_local_candidate_failure_returns_non_success_without_retry(
    controlled, monkeypatch, fault
):
    client, path, counts = controlled
    original_connect = candidate.sqlite3.connect
    original_decoder = candidate.KVault
    held = []

    if fault == "source":

        class Connection(candidate.sqlite3.Connection):
            def close(self):
                raise OSError("source release failed")

        def connect(*args, **kwargs):
            conn = original_connect(
                *args, factory=Connection, check_same_thread=False, **kwargs
            )
            held.append(conn)
            return conn

        original_candidate = candidate.read_metadata_candidate

        def failing_candidate(*args, **kwargs):
            with monkeypatch.context() as scoped:
                scoped.setattr(candidate.sqlite3, "connect", connect)
                return original_candidate(*args, **kwargs)

        monkeypatch.setattr(candidate, "read_metadata_candidate", failing_candidate)
    else:

        class Decoder(original_decoder):
            def close(self):
                raise OSError("decoder release failed")

        monkeypatch.setattr(candidate, "KVault", Decoder)

    try:
        response = client.post("/api/sessions/saved/resume/preflight")
        assert response.status_code == (503 if fault == "source" else 500)
        assert response.json()["detail"]["code"] == (
            "session_metadata_resource_unavailable"
            if fault == "source"
            else "session_metadata_read_failed"
        )
        assert counts["selected"] == 1
        assert counts["classification"] == 2
        assert counts["version"] == 1
        assert counts["ok"] == counts["service"] == 0
        if fault == "source":
            assert GATE.snapshot(path).new_uncertain
        else:
            assert not GATE.snapshot(path).registered
    finally:
        for connection in held:
            candidate.sqlite3.Connection.close(connection)


def test_remote_and_missing_requests_do_not_enter_candidate(controlled):
    client, path, counts = controlled
    response = client.post(
        "/api/sessions/saved/resume/preflight", json={"on_node": "remote"}
    )
    assert response.status_code == 500
    assert counts == {"classification": 2, "service": 1}
    counts.clear()
    response = client.post("/api/sessions/missing/resume/preflight")
    assert response.status_code == 404
    assert not counts
