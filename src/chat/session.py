"""One chat conversation: history, streaming, cost.

Simpler than the voice session - no VAD, no barge-in, no audio clock. A chat
turn is a request and a reply.

The one thing it does that voice does not is **bound its own history**, and
that is deliberate rather than tidy-minded. Every turn is billed its full
accumulated prompt, so cost grows with the square of the conversation: measured
against the plan's assumptions, five turns cost Rs 0.032 each and forty cost
Rs 0.088 each. A phone caller hangs up after three minutes; a chat window stays
open. Trimming is therefore a first-build concern here, not a later
optimisation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import logging
from typing import Any

from src.agent.tools.runner import ToolSession
from src.chat.gemini_text import GeminiTextModel
from src.voice.usage import FLASH_LITE, SessionUsage

log = logging.getLogger(__name__)


@dataclass
class ChatRecord:
    turn: int
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int


@dataclass
class ChatResult:
    turns: list[ChatRecord] = field(default_factory=list)


class ChatSession:
    # Two rounds, because one question can genuinely need two lookups:
    # "20,000-30,000 kulla phone, stock irukka?" is a search and then a stock
    # check on one of its results, and the second depends on the first. A
    # third round would mean the model rejected its own results, which is a
    # prompt problem rather than something to paper over with another call.
    MAX_TOOL_ROUNDS = 2

    def __init__(
        self,
        model: GeminiTextModel,
        tools: ToolSession | None = None,
        max_history_messages: int = 12,
    ) -> None:
        self._model = model
        self._tools = tools
        # Twelve messages is six exchanges. Enough that "what about the other
        # one" still resolves, short enough that the prompt stops growing.
        self._max_history = max_history_messages
        self._history: list[dict[str, Any]] = []
        self.usage = SessionUsage(rates=FLASH_LITE)
        self.result = ChatResult()

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def _trim(self) -> None:
        """Drop the oldest exchanges, keeping whole pairs.

        Cutting mid-pair would leave a reply whose question is gone, which
        reads to the model as the assistant volunteering things unprompted.
        """
        excess = len(self._history) - self._max_history
        if excess > 0:
            self._history = self._history[excess + (excess % 2) :]

    async def send(self, text: str) -> AsyncIterator[str]:
        """Add the customer's message, stream the reply, record what it cost."""
        self._history.append({"role": "user", "parts": [{"text": text}]})
        self._trim()

        reply = ""
        turn = None
        for _round in range(self.MAX_TOOL_ROUNDS + 1):
            reply = ""
            turn = None
            async for delta, streamed in self._model.stream(self._history):
                turn = streamed
                reply += delta
                yield delta

            if turn is None or not (call := turn.function_call):
                break

            # The model asked for a search. Record the ask, run it, record the
            # answer, and let it speak again with real products in hand.
            # The part verbatim, thoughtSignature included: without it the
            # follow-up request is rejected with a 400 saying the signature
            # is required for tools to work.
            self._history.append(
                {
                    "role": "model",
                    "parts": [turn.function_call_part or {"functionCall": call}],
                }
            )
            result = (
                await self._tools.run(call.get("name", ""), call.get("args") or {})
                if self._tools is not None
                else {"error": "catalogue_not_configured"}
            )
            self._history.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": call.get("name", "search_products"),
                                "response": result,
                            }
                        }
                    ],
                }
            )

        self._history.append({"role": "model", "parts": [{"text": reply}]})

        if turn is not None and turn.usage:
            recorded = self.usage.record_text(turn.usage)
            self.result.turns.append(
                ChatRecord(
                    turn=len(self.result.turns) + 1,
                    prompt_tokens=recorded.prompt_text,
                    response_tokens=recorded.response_text,
                    thinking_tokens=recorded.thoughts,
                )
            )
