"""Guard: ``GET /`` (and the static-file fallback) must not block.

Pre-fix the SPA catch-all in ``api.app._mount_spa`` ran sync
filesystem checks inline on the event loop — ``Path.is_file`` + two
``Path.resolve()`` calls per request.  Under concurrent traffic this
stalled other routes; the user observed "GET / sometimes super
costly" and "blocking as well".

These tests pin two properties:

1. ``GET /`` with empty path takes the index-html short-circuit (no
   filesystem walk).
2. The static-path filesystem check runs on the dedicated I/O executor,
   not on the event loop.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from kohakuterrarium.api import app as app_mod
from tests.helpers.loop_witness import LoopWitness


@pytest.fixture
def static_dir(tmp_path):
    """A minimal Vue-build-like static directory."""
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><html>SPA</html>")
    (d / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    assets = d / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("// vue app")
    return d


class TestSpaFallback:
    def test_get_root_serves_index_html(self, static_dir: Path):
        from kohakuterrarium.api.app import _mount_spa

        app = FastAPI()
        _mount_spa(app, static_dir)
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"SPA" in resp.content

    def test_get_real_file_serves_it(self, static_dir: Path):
        from kohakuterrarium.api.app import _mount_spa

        app = FastAPI()
        _mount_spa(app, static_dir)
        client = TestClient(app)
        resp = client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x00\x00\x01\x00")

    def test_unknown_path_falls_through_to_index(self, static_dir: Path):
        """Vue Router's client-side paths return the SPA shell."""
        from kohakuterrarium.api.app import _mount_spa

        app = FastAPI()
        _mount_spa(app, static_dir)
        client = TestClient(app)
        resp = client.get("/some/client/route")
        assert resp.status_code == 200
        assert b"SPA" in resp.content

    def test_traversal_attempt_falls_through_to_index(self, static_dir: Path):
        """``..`` segments must not escape ``static_dir``."""
        from kohakuterrarium.api.app import _mount_spa

        # Plant a file OUTSIDE static_dir to confirm it can't be read.
        outside = static_dir.parent / "secret.txt"
        outside.write_text("secret")
        app = FastAPI()
        _mount_spa(app, static_dir)
        client = TestClient(app)
        resp = client.get("/../secret.txt")
        # Either 404 (path normalised by httpx) or 200 with SPA body —
        # never with the secret content.
        assert resp.status_code == 200
        assert b"secret" not in resp.content


class TestSpaFallbackOffLoadsToExecutor:
    async def test_static_file_check_runs_off_the_loop(self, static_dir, monkeypatch):
        """The static-path ``is_file`` check runs on the I/O executor, so the
        loop stays free for other requests while the filesystem is slow."""
        witness = LoopWitness(asyncio.get_running_loop())
        original_is_file = Path.is_file

        def _observed_is_file(self):
            if self.is_relative_to(static_dir):
                witness.observe()
            return original_is_file(self)

        monkeypatch.setattr(Path, "is_file", _observed_is_file)
        app = FastAPI()
        app_mod._mount_spa(app, static_dir)
        handler = next(
            route.endpoint
            for route in app.routes
            if isinstance(route, APIRoute) and route.path == "/{full_path:path}"
        )
        await handler("favicon.ico")
        assert witness.never_blocked, (
            "spa_fallback ran its filesystem check on the event loop instead of "
            "the I/O executor"
        )
