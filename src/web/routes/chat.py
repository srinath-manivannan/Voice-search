"""WS /ws/chat - one text conversation."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from src.config import Config
from src.web.ratelimit import RateLimiter
from src.web.routes.voice import POLICY_VIOLATION
from src.web.services.chat_relay import ChatRelay

log = logging.getLogger(__name__)

# Chat is far cheaper per message than a call, so it gets a looser limit -
# but not an absent one: it reaches the same tools and the same upstream.
CHAT_MULTIPLIER = 5


def install(app: FastAPI, settings: Config) -> None:
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
