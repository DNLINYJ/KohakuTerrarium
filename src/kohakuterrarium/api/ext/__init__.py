"""Public FastAPI helpers for package-provided extension routes.

A package's ``ui.api`` router is served under ``/api/ext/<package>/``.
Its routes use these dependencies instead of importing ``api.auth`` or
``api.deps`` directly, so the auth boundary stays inside ``api/``::

    from fastapi import APIRouter, Depends
    from kohakuterrarium.api.ext import current_engine, require_admin

    router = APIRouter()

    @router.get("/status")
    async def status(engine=Depends(current_engine)):
        ...
"""

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from kohakuterrarium.api.auth.dependencies import (
    get_auth_config,
    get_optional_user,
    verify_admin_token,
)
from kohakuterrarium.api.auth.models import User
from kohakuterrarium.api.deps import get_service
from kohakuterrarium.modules.ui_owner import OwnerArgumentError, OwnerForbiddenError
from kohakuterrarium.studio.studio import Studio
from kohakuterrarium.terrarium import LocalTerrariumService, TerrariumService

EXT_PACKAGE_SCOPE_KEY = "kt.ext.package"


@dataclass(frozen=True)
class UICallContext:
    """Who is calling an owner; passed as ``context=`` to ``ui_state`` / ``ui_call``.

    ``multi_user`` tells a single-user host (False) from multi-user mode,
    where ``user_id`` is ``None`` for an anonymous caller. ``is_admin``
    uses the same rule as :func:`require_admin`.
    """

    user_id: int | None
    role: str
    owner: str
    creature_id: str
    multi_user: bool
    is_admin: bool


def caller_is_admin(conn: HTTPConnection, user: User | None) -> bool:
    """:func:`require_admin`'s rule as a boolean: admin token and admin role."""
    cfg = get_auth_config(conn)
    if cfg.admin_token_enabled:
        supplied = conn.headers.get("x-admin-token", "")
        if not supplied or not secrets.compare_digest(
            supplied.encode(), cfg.admin_token.encode()
        ):
            return False
    if cfg.multi_user_enabled:
        return user is not None and user.role == "admin"
    return True


def package_name(conn: HTTPConnection) -> str:
    """The installed-package name whose route is handling this request."""
    return str(conn.scope.get(EXT_PACKAGE_SCOPE_KEY, ""))


def current_user(
    user: User | None = Depends(get_optional_user),
) -> User | None:
    """The authenticated user under multi-user mode, otherwise ``None``."""
    return user


def require_user(
    conn: HTTPConnection,
    user: User | None = Depends(get_optional_user),
) -> User | None:
    """Reject anonymous callers when the host requires user accounts."""
    cfg = get_auth_config(conn)
    if cfg.multi_user_enabled and cfg.multi_user == "required" and user is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": "user authentication required",
            },
            headers={"X-Auth-Required": "user"},
        )
    return user


def require_admin(
    request: Request,
    _admin_token: None = Depends(verify_admin_token),
    user: User | None = Depends(get_optional_user),
) -> None:
    """Gate a route on the admin token and, under multi-user mode, the admin role."""
    cfg = get_auth_config(request)
    if cfg.multi_user_enabled and (user is None or user.role != "admin"):
        raise HTTPException(
            status_code=403,
            detail={"error": "admin_only", "message": "admin role required"},
        )


def current_service(
    service: TerrariumService = Depends(get_service),
) -> TerrariumService:
    """The Terrarium service for this request (per-user under multi-user mode)."""
    return service


def current_engine(
    service: TerrariumService = Depends(get_service),
) -> Any:
    """The local engine behind :func:`current_service`.

    On a multi-node host this is the coordination engine, which holds no
    creatures; use :func:`current_service` for creature state there.
    """
    if isinstance(service, LocalTerrariumService):
        return service.engine
    engine = getattr(service, "coordination_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "engine_unavailable", "message": "no local engine"},
        )
    return engine


def studio(service: TerrariumService = Depends(get_service)) -> Studio:
    """A :class:`Studio` facade bound to this request's service."""
    return Studio(service=service)


__all__ = [
    "EXT_PACKAGE_SCOPE_KEY",
    "OwnerArgumentError",
    "OwnerForbiddenError",
    "UICallContext",
    "caller_is_admin",
    "current_engine",
    "current_service",
    "current_user",
    "package_name",
    "require_admin",
    "require_user",
    "studio",
]
