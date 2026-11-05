"""Owner RPC: one HTTP endpoint pair and one push socket for every owner kind.

- ``POST /api/extensions/owner/state`` ``{owner, session_id?, creature_id?}``
  → ``{owner, state}``
- ``POST /api/extensions/owner/call`` ``{owner, session_id?, creature_id?,
  action, args}`` → ``{owner, result}``
- ``WS /ws/extensions/owner``: client sends ``{op: "subscribe", id, owner,
  session_id?, creature_id?}`` / ``{op: "unsubscribe", id}`` / ``{op:
  "ping"}``; server sends ``subscribed`` / ``event`` / ``error`` / ``pong``
  frames, and ``{type: "overflow", dropped}`` after dropping pushes.

Failures carry ``detail: {error, message}``; the codes are listed in
:data:`ERROR_STATUS`.
"""

import asyncio
import collections
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi import WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from kohakuterrarium.api.auth.config import AuthConfig
from kohakuterrarium.api.auth.dependencies import get_auth_config
from kohakuterrarium.api.auth.models import User
from kohakuterrarium.api.deps import get_service
from kohakuterrarium.api.ext import (
    UICallContext,
    caller_is_admin,
    current_user,
    require_user,
)
from kohakuterrarium.api.ext.owners import (
    BUS,
    OwnerDisabledError,
    OwnerNotMultiUserError,
    OwnerUnsupportedError,
    ResolvedOwner,
    resolve_owner,
)
from kohakuterrarium.errors import InvalidRequestError
from kohakuterrarium.modules.ui_owner import (
    OwnerArgumentError,
    OwnerForbiddenError,
    OwnerHasNoUIError,
    OwnerNotLoadedError,
    owner_call,
    owner_state,
)
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()
ws_router = APIRouter()

OUTBOX_LIMIT = 256

ERROR_STATUS: dict[str, int] = {
    "owner_not_loaded": 404,
    "owner_disabled": 404,
    "owner_has_no_ui": 404,
    "owner_forbidden": 403,
    "owner_not_multi_user": 403,
    "bad_request": 400,
    "owner_unsupported": 501,
    "owner_failed": 500,
}


class OwnerRef(BaseModel):
    owner: str
    session_id: str = ""
    creature_id: str = ""


class OwnerCall(OwnerRef):
    action: str = Field(min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)


def _failure(exc: Exception) -> tuple[str, str]:
    """Map an owner failure to ``(error_code, message)``."""
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return ("owner_not_loaded" if exc.status_code == 404 else "bad_request"), detail
    for error_type, code in (
        (OwnerDisabledError, "owner_disabled"),
        (OwnerHasNoUIError, "owner_has_no_ui"),
        (OwnerNotLoadedError, "owner_not_loaded"),
        (OwnerForbiddenError, "owner_forbidden"),
        (OwnerNotMultiUserError, "owner_not_multi_user"),
        (OwnerUnsupportedError, "owner_unsupported"),
        (OwnerArgumentError, "bad_request"),
        (InvalidRequestError, "bad_request"),
    ):
        if isinstance(exc, error_type):
            return code, str(exc)
    return "owner_failed", str(exc) or type(exc).__name__


def _raise(exc: Exception) -> None:
    code, message = _failure(exc)
    if code == "owner_failed":
        logger.error("UI owner failed", error=message, exc_info=exc)
    status = ERROR_STATUS[code]
    if isinstance(exc, HTTPException) and code == "bad_request":
        status = exc.status_code
    raise HTTPException(status_code=status, detail={"error": code, "message": message})


def _context(
    conn: HTTPConnection, user: User | None, resolved: ResolvedOwner
) -> UICallContext:
    return UICallContext(
        user_id=user.id if user else None,
        role=user.role if user else "",
        owner=resolved.owner,
        creature_id=resolved.creature_id,
        multi_user=get_auth_config(conn).multi_user_enabled,
        is_admin=caller_is_admin(conn, user),
    )


async def _resolve(request: Request, service, ref: OwnerRef) -> ResolvedOwner:
    try:
        return await resolve_owner(
            service,
            get_auth_config(request),
            ref.owner,
            ref.session_id,
            ref.creature_id,
        )
    except Exception as e:
        _raise(e)


@router.post("/state")
async def read_owner_state(
    ref: OwnerRef,
    request: Request,
    service=Depends(get_service),
    user: User | None = Depends(require_user),
) -> dict:
    """The owner's ``ui_state()``."""
    resolved = await _resolve(request, service, ref)
    try:
        state = await owner_state(resolved.instance, _context(request, user, resolved))
    except Exception as e:
        _raise(e)
    return {"owner": resolved.owner, "state": state}


@router.post("/call")
async def call_owner(
    req: OwnerCall,
    request: Request,
    service=Depends(get_service),
    user: User | None = Depends(require_user),
) -> dict:
    """Run the owner's ``ui_call(action, args)``."""
    resolved = await _resolve(request, service, req)
    context = _context(request, user, resolved)
    try:
        result = await owner_call(resolved.instance, req.action, req.args, context)
    except Exception as e:
        _raise(e)
    return {"owner": resolved.owner, "result": result}


class BoundedOutbox:
    """Outgoing frames for one socket; on overflow the oldest frames are dropped.

    The next frame after a drop is ``{type: "overflow", dropped: n}`` so the
    client can re-read state it may have missed.
    """

    def __init__(self, limit: int = OUTBOX_LIMIT) -> None:
        self._frames: collections.deque = collections.deque()
        self._limit = limit
        self._ready = asyncio.Event()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._frames)

    def put(self, frame: dict) -> None:
        while len(self._frames) >= self._limit:
            self._frames.popleft()
            self.dropped += 1
        self._frames.append(frame)
        self._ready.set()

    async def get(self) -> dict:
        while not self._frames and not self.dropped:
            self._ready.clear()
            await self._ready.wait()
        if self.dropped:
            frame, self.dropped = {"type": "overflow", "dropped": self.dropped}, 0
            return frame
        return self._frames.popleft()


class OwnerSocketSession:
    """One push socket: subscriptions, a bounded outbox, and its sender."""

    def __init__(
        self, websocket: Any, service: Any, auth_config: AuthConfig, user: User | None
    ) -> None:
        self.websocket = websocket
        self.service = service
        self.auth_config = auth_config
        self.user = user
        self.outbox = BoundedOutbox()
        self.subscriptions: dict[str, tuple[tuple, int]] = {}

    async def _pump(self) -> None:
        while True:
            frame = await self.outbox.get()
            await self.websocket.send_text(json.dumps(frame, default=str))

    def _drop(self, sub_id: str) -> None:
        entry = self.subscriptions.pop(sub_id, None)
        if entry is not None:
            BUS.unsubscribe(*entry)

    async def _subscribe(self, sub_id: str, message: dict) -> None:
        self._drop(sub_id)
        try:
            resolved = await resolve_owner(
                self.service,
                self.auth_config,
                str(message.get("owner") or ""),
                str(message.get("session_id") or ""),
                str(message.get("creature_id") or ""),
            )
            # Reading state once lets the owner refuse this subscriber.
            await owner_state(
                resolved.instance, _context(self.websocket, self.user, resolved)
            )
        except Exception as e:
            code, text = _failure(e)
            if code == "owner_failed":
                logger.error("UI owner failed", error=text, exc_info=e)
            self.outbox.put(
                {"type": "error", "id": sub_id, "error": code, "message": text}
            )
            return

        def deliver(event: str, data: Any) -> None:
            self.outbox.put(
                {"type": "event", "id": sub_id, "event": event, "data": data}
            )

        token = BUS.subscribe(
            resolved.key,
            deliver,
            user_id=self.user.id if self.user else None,
            multi_user=self.auth_config.multi_user_enabled,
        )
        self.subscriptions[sub_id] = (resolved.key, token)
        self.outbox.put({"type": "subscribed", "id": sub_id, "owner": resolved.owner})

    async def _handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            message = None
        if not isinstance(message, dict):
            self.outbox.put(
                {
                    "type": "error",
                    "id": "",
                    "error": "bad_request",
                    "message": "invalid JSON",
                }
            )
            return
        op = message.get("op")
        sub_id = str(message.get("id") or "")
        if op == "subscribe" and sub_id:
            await self._subscribe(sub_id, message)
        elif op == "unsubscribe":
            self._drop(sub_id)
        elif op == "ping":
            self.outbox.put({"type": "pong"})
        else:
            self.outbox.put(
                {
                    "type": "error",
                    "id": sub_id,
                    "error": "bad_request",
                    "message": f"unknown op {op!r}",
                }
            )

    async def run(self) -> None:
        """Serve until the client leaves or the sender dies; always cleans up."""
        pump = asyncio.create_task(self._pump())
        try:
            while True:
                receive = asyncio.ensure_future(self.websocket.receive_text())
                await asyncio.wait({receive, pump}, return_when=asyncio.FIRST_COMPLETED)
                if pump.done():
                    receive.cancel()
                    if not pump.cancelled() and pump.exception() is not None:
                        logger.info(
                            "Owner socket sender stopped", error=str(pump.exception())
                        )
                    return
                await self._handle(receive.result())
        except WebSocketDisconnect:
            return
        finally:
            for sub_id in list(self.subscriptions):
                self._drop(sub_id)
            pump.cancel()


@ws_router.websocket("/ws/extensions/owner")
async def owner_events(
    websocket: WebSocket,
    user: User | None = Depends(current_user),
) -> None:
    """Multiplexed push channel: one socket carries every owner subscription."""
    try:
        require_user(websocket, user)
        service = get_service(websocket, user)
    except HTTPException:
        await websocket.close(code=4401, reason="user authentication required")
        return
    await websocket.accept()
    await OwnerSocketSession(websocket, service, get_auth_config(websocket), user).run()
