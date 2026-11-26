"""WS /ws/chat - one text conversation."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from src.config import Config
from src.web.ratelimit import RateLimiter
from src.web.routes.voice import POLICY_VIOLATION
from src.web.services.chat_relay import ChatRelay
from src.web.services.chat_v2 import WidgetRelay, client_ip, socket_origin_allowed

log = logging.getLogger(__name__)

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
        await ChatRelay(websocket, settings, websocket.app.state.reporter).run()


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
