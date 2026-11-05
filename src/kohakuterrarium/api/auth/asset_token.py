"""Short-lived, per-package tokens that let a browser load package UI files.

A dynamic ``import()`` or ``<link>`` cannot send the host-token header, so
the token travels as a path segment of the asset URL
(``/api/ext/<pkg>/ui/<token>/<file>``). Keeping it in the path means
relative imports inside a package resolve to URLs that still carry it.
A token is signed for one package and grants read access to its UI files.
"""

import base64
import hashlib
import hmac
import re
import secrets
import time

from starlette.types import Scope

ASSET_TOKEN_TTL_SECONDS = 6 * 3600
UI_ASSET_PATH_RE = re.compile(
    r"^/api/ext/(?P<pkg>[^/]+)/ui/(?P<token>[^/]+)/(?P<rest>.+)$"
)


def new_asset_secret() -> bytes:
    """A fresh per-process signing secret."""
    return secrets.token_bytes(32)


def _sign(secret: bytes, package: str, expires_at: int) -> str:
    message = f"kt-ui-asset:{package}:{expires_at}".encode()
    digest = hmac.new(secret, message, hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()[:18]).decode().rstrip("=")


def mint_asset_token(
    secret: bytes,
    package: str,
    *,
    now: float | None = None,
    ttl: int = ASSET_TOKEN_TTL_SECONDS,
) -> tuple[str, int]:
    """Return ``(token, expires_at)`` for ``package``'s UI asset URLs."""
    expires_at = int((time.time() if now is None else now) + ttl)
    return f"{expires_at:x}.{_sign(secret, package, expires_at)}", expires_at


def verify_asset_token(
    secret: bytes, token: str, package: str, *, now: float | None = None
) -> bool:
    """True when ``token`` was minted for ``package`` and has not expired."""
    exp_hex, _, sig = token.partition(".")
    if not exp_hex or not sig:
        return False
    try:
        expires_at = int(exp_hex, 16)
    except ValueError:
        return False
    if expires_at < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig, _sign(secret, package, expires_at))


def asset_secret_from_scope(scope: Scope) -> bytes | None:
    """The signing secret stored on the serving application's state."""
    state = getattr(scope.get("app"), "state", None)
    secret = getattr(state, "ui_asset_secret", None)
    return secret if isinstance(secret, bytes) else None


def scope_has_valid_asset_token(scope: Scope) -> bool:
    """True for a GET/HEAD UI asset request carrying that package's token."""
    if scope.get("type") != "http":
        return False
    if scope.get("method", "").upper() not in ("GET", "HEAD"):
        return False
    match = UI_ASSET_PATH_RE.match(scope.get("path", ""))
    if match is None:
        return False
    secret = asset_secret_from_scope(scope)
    return secret is not None and verify_asset_token(
        secret, match.group("token"), match.group("pkg")
    )
