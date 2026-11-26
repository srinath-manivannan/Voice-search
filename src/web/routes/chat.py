"""WS /ws/chat - one text conversation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from src.config import Config
from src.web.ratelimit import RateLimiter
from src.web.routes.voice import POLICY_VIOLATION
from src.web.services.chat_relay import ChatRelay
from src.web.services.chat_v2 import WidgetRelay, client_ip, socket_origin_allowed

log = logging.getLogger(__name__)

# A v1 caller waits for `ready` and sends nothing first. A v2 caller sends
# hello as soon as the socket opens. This is long enough for that hello to
# arrive on a local connection, and short enough that a legacy caller is not
# left staring at a blank thread.
_V2_PEEK_SECONDS = 0.4

# Chat is far cheaper per message than a call, so it gets a looser limit -
# but not an absent one: it reaches the same tools and the same upstream.
CHAT_MULTIPLIER = 5


def install(app: FastAPI, settings: Config) -> None:
    if settings.app_mode == "widget":
        _install_v2(app, settings)
        return
    limiter = RateLimiter(
        max_per_minute=settings.max_calls_per_minute * CHAT_MULTIPLIER
    )

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client else "unknown"
        if not limiter.allow(client):
            log.warning("chat refused: %s is over the limit", client)
            await websocket.close(code=POLICY_VIOLATION, reason="too many sessions")
            return
        await websocket.accept()
        if _query_is_v2(websocket):
            await _serve_v2(websocket, settings)
            return
        first = await _peek_first(websocket)
        if first is _PEEK_TIMEOUT:
            await ChatRelay(websocket, settings, websocket.app.state.reporter).run()
            return
        if first is None:
            return
        if _hello_v2(first):
            await _serve_v2(websocket, settings, hello=first)
            return
        await ChatRelay(websocket, settings, websocket.app.state.reporter).run(
            first if isinstance(first, dict) else None
        )


def _install_v2(app: FastAPI, settings: Config) -> None:
    limit = settings.max_calls_per_minute * CHAT_MULTIPLIER

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if not socket_origin_allowed(origin, settings):
            # Accept, then close. A close before the handshake completes
            # leaves uvicorn answering HTTP 403, and the 1008 never reaches
            # the client. The code is the part a canary can check.
            log.warning("widget chat refused: origin is not this service")
            await websocket.accept()
            await websocket.close(code=POLICY_VIOLATION)
            return
        if not settings.widget_enabled:
            # widget.enabled is off. 1013 tells the client to try later
            # without accepting a conversation.
            log.warning("widget chat refused: widget is switched off")
            await websocket.close(code=1013)
            return
        fallback = websocket.client.host if websocket.client else "unknown"
        client = client_ip(websocket.headers, fallback, settings.trust_proxy)
        store = websocket.app.state.conversations
        if not await store.allow("chat", client, limit):
            log.warning("widget chat refused: %s is over the limit", client)
            await websocket.close(code=POLICY_VIOLATION, reason="too many sessions")
            return
        await websocket.accept()
        await WidgetRelay(
            websocket, settings, store, websocket.app.state.reporter, client
        ).run()


_PEEK_TIMEOUT = object()


def _query_is_v2(websocket: WebSocket) -> bool:
    return websocket.query_params.get("v") == "2"


def _hello_v2(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("type") == "hello"
        and payload.get("v") == 2
    )


async def _peek_first(websocket: WebSocket) -> Any:
    """The first frame, or `_PEEK_TIMEOUT` when the caller is waiting on us."""
    try:
        return await asyncio.wait_for(
            websocket.receive_json(), timeout=_V2_PEEK_SECONDS
        )
    except asyncio.TimeoutError:
        return _PEEK_TIMEOUT
    except WebSocketDisconnect:
        return None
    except Exception:
        log.info("chat closed before a protocol frame arrived")
        try:
            await websocket.close(code=POLICY_VIOLATION)
        except Exception:
            return None
        return None


async def _serve_v2(
    websocket: WebSocket, settings: Config, hello: dict[str, Any] | None = None
) -> None:
    """Protocol v2 on the full app, using the same conversation store."""
    limit = settings.max_calls_per_minute * CHAT_MULTIPLIER
    fallback = websocket.client.host if websocket.client else "unknown"
    client = client_ip(websocket.headers, fallback, settings.trust_proxy)
    store = websocket.app.state.conversations
    if not await store.allow("chat", client, limit):
        log.warning("chat v2 refused: %s is over the limit", client)
        await websocket.close(code=POLICY_VIOLATION, reason="too many sessions")
        return
    await WidgetRelay(
        websocket, settings, store, websocket.app.state.reporter, client
    ).run(hello)
