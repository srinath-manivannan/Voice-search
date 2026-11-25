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
from src.observability.reporting import Reporter
from src.web.services import dependencies
from src.web.services.cost_report import spent_inr

log = logging.getLogger(__name__)

# Said instead of a paid answer once a conversation has reached its cap.
CAPPED_REPLY = (
    "இந்த உரையாடலுக்கான அளவு முடிஞ்சது. Guided-ல இலவசமா தொடரலாம். "
    "This chat has reached its limit - please continue in Guided, at no cost."
)


class ChatRelay:
    def __init__(
        self, websocket: WebSocket, settings: Config, reporter: Reporter
    ) -> None:
        self._socket = websocket
        self._settings = settings
        self._reporter = reporter
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
        except Exception as failed:
            self._reporter.capture(
                failed, where="chat", turns=len(self._session.result.turns)
            )
            with contextlib.suppress(Exception):
                await self._socket.send_json(
                    {"type": "error", "message": "that message could not be answered"}
                )

    async def _answer(self, text: str) -> None:
        spent = spent_inr(self._session.usage, self._settings)
        ledger = getattr(getattr(self._socket, "app", None), "state", None)
        ledger = getattr(ledger, "ledger", None)
        if spent >= self._settings.chat_session_cap_inr or (
            ledger is not None and not ledger.allows()
        ):
            # No model call at all past the cap: a fixed line, and the way
            # to the free Guided call.
            await self._socket.send_json({"type": "start"})
            await self._socket.send_json({"type": "delta", "text": CAPPED_REPLY})
            await self._socket.send_json(
                {
                    "type": "done",
                    "turn": None,
                    "capped": True,
                    "conversation_inr": spent,
                }
            )
            return
        await self._socket.send_json({"type": "start"})
        async for delta in self._session.send(text):
            await self._socket.send_json({"type": "delta", "text": delta})

        if ledger is not None:
            ledger.add(spent_inr(self._session.usage, self._settings) - spent)
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
