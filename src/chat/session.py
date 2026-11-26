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

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
import logging
from typing import Any, Protocol

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


def _is_question(message: dict[str, Any]) -> bool:
    """A message the customer typed, rather than an answer to a tool call.

    Both carry role "user" - the functionResponse is sent as one - so the
    parts are what tells them apart.
    """
    if message.get("role") != "user":
        return False
    return any("text" in part for part in message.get("parts") or [])


# Reading what the caller wants out of Tanglish that arrives misspelt,
# half-finished and romanised three different ways. Some slack, because the
# input varies and what comes out is a tool call rather than a claim.
READING_TEMPERATURE = 0.2
# Answering with the tool's result already in the window. Every number it
# could need is in front of it, so there is nothing to be creative about -
# and creativity at this point is the word for a price nobody quoted.
#
# Temperature costs nothing either way. The same tokens are generated and
# billed at the same rate at 0.0 as at 1.0; this is a hallucination control
# and it is only in this file because that is where the two halves of a turn
# can be told apart for free.
QUOTING_TEMPERATURE = 0.0


class RoundHooks(Protocol):
    """Reserve before a generation and settle after it, including tool rounds."""

    async def before(self, round_index: int) -> bool: ...

    async def after(self, round_index: int, usage: dict[str, Any]) -> None: ...

    async def failed(self, round_index: int) -> None: ...


Sanitise = Callable[[dict[str, Any]], dict[str, Any]]


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
        sanitise: Sanitise | None = None,
        hooks: RoundHooks | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        # Twelve messages is six exchanges. Enough that "what about the other
        # one" still resolves, short enough that the prompt stops growing.
        self._max_history = max_history_messages
        self._history: list[dict[str, Any]] = []
        self._sanitise = sanitise
        self._hooks = hooks
        self.usage = SessionUsage(rates=FLASH_LITE)
        self.result = ChatResult()
        # "cap" when a reservation was refused before a generation.
        self.stopped: str | None = None

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def remember(self, context: str) -> None:
        """Put something in the history without asking for an answer.

        What `GeminiLiveSession.prime` does with `turnComplete: false`, done
        by the side that owns the history instead of by the wire protocol.
        The acknowledgement is ours, not the model's: a pair is appended so
        `_trim` still sees whole exchanges, and so the next real question is
        not read as a second message from the customer in a row.

        Used by the prefetch, which hands over a shortlist the customer has
        not asked for yet. Priming must never make the agent say anything.
        """
        if not context.strip():
            return
        self._history.append({"role": "user", "parts": [{"text": context}]})
        self._history.append({"role": "model", "parts": [{"text": "Sari."}]})
        self._trim()

    def _trim(self) -> None:
        """Drop the oldest exchanges, keeping whole pairs.

        Cutting mid-pair would leave a reply whose question is gone, which
        reads to the model as the assistant volunteering things unprompted.

        An even-numbered cut was enough until the tools arrived. A tool round
        adds two more messages - the model's functionCall and the answer to
        it - and a cut that lands between them leaves a functionResponse
        whose call is gone. The API rejects the entire request for it:

            Please ensure that function response turn comes immediately
            after a function call turn.

        Four questions into a conversation, the chat tab stopped answering
        anything at all. So the cut is advanced to the next plain question,
        which is the only kind of message a conversation may begin with. The
        caller's newest question is always in the history when this runs, so
        there is always one to find.
        """
        excess = len(self._history) - self._max_history
        if excess <= 0:
            return
        cut = excess + (excess % 2)
        while cut < len(self._history) and not _is_question(self._history[cut]):
            cut += 1
        self._history = self._history[cut:]

    async def send(self, text: str) -> AsyncIterator[str]:
        """Add the customer's message, stream the reply, record what it cost."""
        self._history.append({"role": "user", "parts": [{"text": text}]})
        self._trim()

        reply = ""
        turn = None
        for _round in range(self.MAX_TOOL_ROUNDS + 1):
            if self._hooks is not None and not await self._hooks.before(_round):
                self.stopped = "cap"
                break
            reply = ""
            turn = None
            # The round IS the router, and it costs nothing to ask: round
            # zero is the model reading the caller, every round after it is
            # the model quoting something a tool returned.
            temperature = READING_TEMPERATURE if _round == 0 else QUOTING_TEMPERATURE
            try:
                async for delta, streamed in self._model.stream(
                    self._history, temperature
                ):
                    turn = streamed
                    reply += delta
                    yield delta
            except Exception:
                if self._hooks is not None:
                    await self._hooks.failed(_round)
                raise

            usage = turn.usage if turn is not None and turn.usage else {}
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
            if self._hooks is not None:
                await self._hooks.after(_round, usage)

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
            if self._sanitise is not None:
                result = self._sanitise(
                    result
                    if isinstance(result, dict)
                    else {"error": "details_unavailable"}
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
