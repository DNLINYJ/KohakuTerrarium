"""Unit tests for ``api/auth/asset_token.py`` and its host-token bypass."""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from kohakuterrarium.api.auth.asset_token import (
    UI_ASSET_PATH_RE,
    mint_asset_token,
    new_asset_secret,
    scope_has_valid_asset_token,
    verify_asset_token,
)
from kohakuterrarium.api.auth.config import AuthConfig
from kohakuterrarium.api.auth.middleware import HostTokenMiddleware


class TestTokens:
    def test_round_trip_expiry_and_package_scope(self):
        secret = new_asset_secret()
        token, expires_at = mint_asset_token(secret, "alpha", now=1000.0, ttl=60)
        assert expires_at == 1060
        assert verify_asset_token(secret, token, "alpha", now=1059.0)
        assert not verify_asset_token(secret, token, "alpha", now=1061.0)
        assert not verify_asset_token(secret, token, "beta", now=1000.0)

    def test_tampering_and_foreign_secret_fail(self):
        secret = new_asset_secret()
        token, _ = mint_asset_token(secret, "p", now=1000.0, ttl=60)
        exp, _, sig = token.partition(".")
        later = f"{int(exp, 16) + 3600:x}.{sig}"
        assert not verify_asset_token(secret, later, "p", now=1000.0)
        assert not verify_asset_token(new_asset_secret(), token, "p", now=1000.0)
        for bad in ("", "nodot", ".sig", "zz.sig"):
            assert not verify_asset_token(secret, bad, "p", now=1000.0)

    def test_path_pattern(self):
        m = UI_ASSET_PATH_RE.match("/api/ext/pkg/ui/TOKEN/sub/a.js")
        assert m and m.group("pkg") == "pkg" and m.group("rest") == "sub/a.js"
        assert UI_ASSET_PATH_RE.match("/api/ext/pkg/status") is None
        assert UI_ASSET_PATH_RE.match("/api/ext/pkg/ui/TOKEN") is None

    def test_scope_check_needs_get_app_secret_and_matching_package(self):
        secret = new_asset_secret()
        token, _ = mint_asset_token(secret, "p")
        app = FastAPI()
        app.state.ui_asset_secret = secret
        base = {"type": "http", "method": "GET", "app": app}
        path = f"/api/ext/p/ui/{token}/index.js"
        assert scope_has_valid_asset_token({**base, "path": path})
        assert not scope_has_valid_asset_token(
            {**base, "path": f"/api/ext/q/ui/{token}/a.js"}
        )
        assert not scope_has_valid_asset_token({**base, "path": path, "method": "POST"})
        assert not scope_has_valid_asset_token({**base, "path": "/api/ext/p/x"})
        assert not scope_has_valid_asset_token(
            {**base, "type": "websocket", "path": path}
        )
        assert not scope_has_valid_asset_token(
            {"type": "http", "method": "GET", "path": path}
        )


def _app() -> tuple[FastAPI, bytes]:
    app = FastAPI()
    app.state.auth_config = AuthConfig(host_token="HOST", loopback_bypass=False)
    app.state.ui_asset_secret = new_asset_secret()
    router = APIRouter()

    @router.get("/api/ext/{pkg}/ui/{token}/{path:path}")
    def asset(pkg: str, token: str, path: str) -> dict:
        return {"pkg": pkg, "path": path}

    @router.get("/api/ext/{pkg}/data")
    def data(pkg: str) -> dict:
        return {"pkg": pkg}

    app.include_router(router)
    app.add_middleware(HostTokenMiddleware)
    return app, app.state.ui_asset_secret


class TestMiddlewareBypass:
    def test_valid_token_opens_its_own_package_assets_only(self):
        app, secret = _app()
        token, _ = mint_asset_token(secret, "p")
        with TestClient(app) as client:
            assert client.get(f"/api/ext/p/ui/{token}/a.js").status_code == 200
            assert client.get(f"/api/ext/q/ui/{token}/a.js").status_code == 401
            assert client.get("/api/ext/p/data").status_code == 401
            assert client.get(f"/api/ext/p/data?t={token}").status_code == 401
            assert client.get("/api/ext/p/ui/bad.sig/a.js").status_code == 401
            resp = client.get(
                "/api/ext/p/ui/bad.sig/a.js", headers={"X-KT-Host-Token": "HOST"}
            )
            assert resp.status_code == 200

    def test_expired_token_is_rejected(self):
        app, secret = _app()
        token, _ = mint_asset_token(secret, "p", now=0.0, ttl=1)
        with TestClient(app) as client:
            assert client.get(f"/api/ext/p/ui/{token}/a.js").status_code == 401
