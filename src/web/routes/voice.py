"""WS /ws - one browser voice call.

Accept, rate-limit, hand to the service. Everything the call DOES is in
`src/web/services/voice_call.py`.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from src.config import Config
from src.web.ratelimit import RateLimiter
from src.web.services.voice_call import VoiceCall

log = logging.getLogger(__name__)

# 1008 is "policy violation", which is what a refused call is. Closing with a
# reason means the page can say why instead of showing a dead line.
POLICY_VIOLATION = 1008


def install(app: FastAPI, settings: Config) -> None:
    limiter = RateLimiter(max_per_minute=settings.max_calls_per_minute)

    @app.websocket("/ws")
    async def conversation(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client else "unknown"
        if not limiter.allow(client):
            log.warning(
                "call refused: %s is over %d a minute",
                client,
                settings.max_calls_per_minute,
            )
            await websocket.close(code=POLICY_VIOLATION, reason="too many calls")
            return
        await websocket.accept()
        await VoiceCall(websocket, settings, websocket.app.state.reporter).run()
