"""The one call a guided session makes to a model.

Everything before this in a session is a list being narrowed by taps. This
is the only place judgement is bought, and it is bought once: given what the
customer chose and up to twelve products that are all in stock and all
inside the budget, which five, in what order, and why.

Measured 2026-11-23 on `gemini-3.5-flash-lite`: 1,192 tokens in, 134 out,
none of them thinking, 1.1-1.5 seconds, Rs 0.073 with GST. The next model up
took 9.2 seconds to give a ranking no better, which is over the five seconds
a customer was promised.

What the model is NOT trusted with is in `recommend.py`: it returns ids and
a sentence each, and code attaches every name, price and stock figure. It
never sees a price it could misquote as a discount, because the instruction
tells it not to mention one and the check afterwards does not rely on that.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.guided.model import HOST, JsonModel, ModelUnavailable, Poster, usd_for
from src.guided.shelf import Item
from src.voice.cost import FLASH_LITE, Rates

__all__ = [
    "HOST",
    "Brief",
    "GeminiThinker",
    "Thinker",
    "ThinkerUnavailable",
    "Thought",
    "usd_for",
]

# The most the reply may run to. Five picks at a short sentence each came to
# 134 tokens; this is room for Tamil, which tokenises long, and a hard stop
# for a model that decides to write an essay.
MAX_OUTPUT_TOKENS = 500
# The most the request can run to, by construction: twelve products, names
# cut at `NAME_CHARS`, a handful of facts each, and the instruction below.
# Deliberately generous. The governor admits a think against the WORST case
# rather than the usual one, so the ceiling cannot be reached by a prompt
# that came out longer than expected.
MOST_PROMPT_TOKENS = 2_500
NAME_CHARS = 90

INSTRUCTION = """Rank the listed Poorvika SKUs for the confirmed shopping brief. Keep hard constraints and prioritise the requested uses in order. Use only each SKU's own evidence. Return up to four IDs best first; why may be empty. Reply in {language}."""

_WHY_LANGUAGE = {"ta": "Tamil script", "en": "simple English"}


def instruction_for(lang: str) -> str:
    return INSTRUCTION.format(language=_WHY_LANGUAGE.get(lang, "Tamil script"))


_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "picks": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"id": {"type": "STRING"}, "why": {"type": "STRING"}},
                "required": ["id", "why"],
            },
        }
    },
    "required": ["picks"],
}


# One name for "the model could not be asked", whichever question it was.
ThinkerUnavailable = ModelUnavailable


@dataclass(frozen=True)
class Brief:
    """What the model is told. Small, and nothing in it is the customer's own."""

    shelf: str
    # (what was asked, what was answered), in the order it was answered.
    wants: tuple[tuple[str, str], ...]
    products: tuple[Item, ...]
    # Which language the "why" sentences are written in.
    lang: str = "ta"


@dataclass(frozen=True)
class Thought:
    """What came back, and what it cost whether or not it was any use."""

    picks: tuple[tuple[str, str], ...]
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int
    usd: float
    took_ms: int


class Thinker(Protocol):
    async def rank(self, brief: Brief) -> Thought: ...


# What a think is admitted against. See `MOST_PROMPT_TOKENS`.
WORST_CASE_USD = usd_for(MOST_PROMPT_TOKENS, MAX_OUTPUT_TOKENS, 0)


def request_for(brief: Brief) -> dict[str, Any]:
    """The request body. A function of the brief alone, so it can be tested."""
    asked = {
        "shelf": brief.shelf,
        "shopper_chose": {question: answer for question, answer in brief.wants},
        "products": [
            {
                "id": str(number),
                "name": item.name[:NAME_CHARS],
                "facts": {label: value for label, value in item.specs},
                **({"rating_out_of_5": item.rating} if item.rating else {}),
            }
            for number, item in enumerate(brief.products, start=1)
        ],
    }
    return JsonModel.request(
        instruction_for(brief.lang),
        json.dumps(asked, ensure_ascii=False),
        _SCHEMA,
        MAX_OUTPUT_TOKENS,
    )


def read_reply(data: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """(id, why) pairs out of the model's reply, or nothing it can be read as."""
    picks = (data or {}).get("picks")
    if not isinstance(picks, list):
        raise ThinkerUnavailable("the reply was not the picks asked for")
    return tuple(
        (str(pick.get("id") or "").strip(), str(pick.get("why") or "").strip())
        for pick in picks
        if isinstance(pick, dict)
    )


class GeminiThinker:
    def __init__(
        self,
        api_key: str,
        model: str,
        rates: Rates = FLASH_LITE,
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._model = JsonModel(api_key, model, rates, poster_factory)

    async def rank(self, brief: Brief) -> Thought:
        asked = request_for(brief)
        from src.guided.store import reserve_rank

        # UTF-8 bytes bound text tokens conservatively. Include schema/instructions and
        # framing. If the verified brief cannot fit, use local ranking.
        payload = json.loads(asked["contents"][0]["parts"][0]["text"])
        while (
            len(json.dumps(asked, ensure_ascii=False).encode()) + 64
            > MOST_PROMPT_TOKENS
        ):
            if len(payload["products"]) <= 4:
                raise ThinkerUnavailable("brief exceeds the session limit")
            payload["products"].pop()
            asked["contents"][0]["parts"][0]["text"] = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
        reserve = reserve_rank.get()
        if reserve is not None and not await reserve():
            raise ThinkerUnavailable("already reserved")
        reply = await self._model.ask(
            instruction_for(brief.lang),
            asked["contents"][0]["parts"][0]["text"],
            _SCHEMA,
            MAX_OUTPUT_TOKENS,
        )
        try:
            picks = read_reply(reply.data)
        except ThinkerUnavailable:
            # Unreadable, and already paid for. The caller has to know what
            # was spent even when nothing usable came back.
            picks = ()
        return Thought(
            picks,
            reply.prompt_tokens,
            reply.response_tokens,
            reply.thinking_tokens,
            reply.usd,
            reply.took_ms,
        )
