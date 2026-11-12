"""One chat conversation over a socket.

A socket of its own rather than message types on the voice socket. The voice
socket carries fifty binary audio frames a second; multiplexing a text
conversation onto it would couple two unrelated lifecycles for no gain, since
the UI shows one tab at a time.

Everything about what the agent SAYS lives in `src/chat/session.py`. This
only moves it between that session and a browser.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import WebSocket, WebSocketDisconnect

from src.agent.persona import GREETING
from src.chat.gemini_text import GeminiTextModel
from src.chat.session import ChatSession
from src.config import Config
from src.web.services import dependencies
from src.web.services.cost_report import spent_inr

log = logging.getLogger(__name__)


class ChatRelay:
    def __init__(self, websocket: WebSocket, settings: Config) -> None:
        self._socket = websocket
        self._settings = settings
        self._session = ChatSession(
            GeminiTextModel(
                api_key=settings.gemini_api_key,
                model=settings.gemini_text_model,
                system_instruction=settings.chat_instruction,
                tools=dependencies.declarations_for(settings),
            ),
            tools=dependencies.tool_session(settings),
        )

    async def run(self) -> None:
        try:
            await self._socket.send_json({"type": "ready", "greeting": GREETING})
            while True:
                message = await self._socket.receive_json()
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                await self._answer(text)
        except WebSocketDisconnect:
            log.info("chat disconnected")
        except Exception:
            log.exception("chat failed")
            with contextlib.suppress(Exception):
                await self._socket.send_json(
                    {"type": "error", "message": "that message could not be answered"}
                )

    async def _answer(self, text: str) -> None:
        await self._socket.send_json({"type": "start"})
        async for delta in self._session.send(text):
            await self._socket.send_json({"type": "delta", "text": delta})

        record = self._session.result.turns[-1] if self._session.result.turns else None
        await self._socket.send_json(
            {
                "type": "done",
                "turn": record.turn if record else None,
                "prompt_tokens": record.prompt_tokens if record else 0,
                "response_tokens": record.response_tokens if record else 0,
                "thinking_tokens": record.thinking_tokens if record else 0,
                "conversation_inr": spent_inr(self._session.usage, self._settings),
            }
        )
