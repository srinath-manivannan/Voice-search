"""What the customer said, turned into the buttons it presses.

Speech is another way to tap. The customer can say "the second one",
"Samsung, under thirty thousand, for college", "is there EMI?" - and the
call presses the buttons those words mean, exactly as if they had been
tapped. Nothing else: **speech can only press a button that exists.** The
ear is given the buttons on screen, the other things this shelf can be told,
and the few commands there are, and returns ids. Code then presses each one
through the same check a tap goes through, so an id that is not a real
button does nothing, however it got there.

The one exception is a question about the product on the card that is not
one of its buttons ("is the camera good in low light?"). That goes to the
answerer, which answers only from Poorvika's own facts, checks every figure,
and is capped at six a session - the same limits the buttons have.

One request does the hearing and the pressing: audio in, ids out. Measured
alone, hearing is about a paisa; the menu adds a few hundred tokens of text.
Every hear is admitted by the governor first, eight seconds at most, twenty
a session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.guided.listen import audio_part
from src.guided.model import ModelUnavailable, usd_for

log = logging.getLogger(__name__)

RATE = 16_000
LEAST_SECONDS = 0.4
MOST_SECONDS = 8.0
MOST_BYTES = int(MOST_SECONDS * RATE * 2) + 4_096
LEAST_BYTES = int(LEAST_SECONDS * RATE * 2)
MOST_TURNS = 20
MOST_PRESSES = 6
MAX_OUTPUT_TOKENS = 300
# By construction: eight seconds of audio, a menu of a few dozen buttons.
MOST_PROMPT_TOKENS = int(MOST_SECONDS * 25) + 1_800
WORST_CASE_USD = usd_for(MOST_PROMPT_TOKENS, MAX_OUTPUT_TOKENS, 0)

# What a press may name besides a question of the shelf.
PICK, ASK, COMMAND = "pick", "ask", "command"
BACK, RESTART, CHEAPER, MORE = "back", "restart", "cheaper", "more"
# The suggestion walk: another one, or lay out the list.
NEXT, LIST = "next", "list"

INSTRUCTION = """\
You are the ears of a shop's touch-screen menu at Poorvika, an electronics \
retailer. The recording is a shopper speaking, in Tamil, English or a mix. \
You are given the menu as JSON. Every item in it is a button, and carries \
the "question" and "id" that press it.

Decide which buttons the shopper's words press. Each press is \
{"question": <the item's question>, "choice": <the item's id>}, copied \
exactly from one item of the menu.

- Never return a question or id that is not on an item in the menu.
- "the second one", "rendavathu", "2" means the second item of the list on \
screen: the second product card if cards are shown, otherwise the second \
button of the question on screen.
- A number of rupees means the budget item it falls in.
- A shopper may answer several things at once ("Samsung, under 30 thousand, \
for college"): return one press for each, in the order they said them.
- "yes", "sari", "ok", "aama" when a suggestion is on screen presses its \
first button.
- If the menu offers a choice of languages, the language the shopper SPOKE \
presses it: Tamil or Tanglish presses "ta", English presses "en". They do \
not have to name a language.
- If they ask something about the product on screen that no item covers, \
put their question, in their words, in "question". Otherwise leave it empty.
- If nothing they said matches an item, return no presses.

Reply with JSON only:
{"heard": "<what they said>", "presses": [{"question": "<question>", "choice": "<id>"}], \
"question": "<their question or empty>"}"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "heard": {"type": "STRING"},
        "presses": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "choice": {"type": "STRING"},
                },
                "required": ["question", "choice"],
            },
        },
        "question": {"type": "STRING"},
    },
    "required": ["heard", "presses", "question"],
}


class Calls(Protocol):
    """The slice of `listen.Ear` this needs."""

    async def call(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]: ...


@dataclass(frozen=True)
class Press:
    question: str
    choice: str


@dataclass(frozen=True)
class Heard:
    text: str
    presses: tuple[Press, ...] = ()
    question: str = ""
    usd: float = 0.0
    took_ms: int = 0
    understood: bool = True
    notes: tuple[str, ...] = field(default=())


def wav16(pcm: bytes) -> bytes:
    """Mono 16-bit PCM at 16 kHz with the header the model needs."""
    from src.guided.voice import wav

    return wav(pcm, RATE)


def request(menu: dict[str, Any], pcm: bytes) -> dict[str, Any]:
    """The body sent. A function of the menu and the audio, so it is testable."""
    return {
        "systemInstruction": {"parts": [{"text": INSTRUCTION}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    audio_part(wav16(pcm)),
                    {"text": json.dumps({"menu": menu}, ensure_ascii=False)},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }


def read(reply: dict[str, Any]) -> tuple[str, tuple[Press, ...], str] | None:
    try:
        parts = reply["candidates"][0]["content"]["parts"]
        data = json.loads("".join(str(p.get("text") or "") for p in parts))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    presses = tuple(
        Press(str(p.get("question") or "")[:24], str(p.get("choice") or "")[:80])
        for p in (data.get("presses") or [])[:MOST_PRESSES]
        if isinstance(p, dict)
    )
    return (
        str(data.get("heard") or "").strip()[:300],
        presses,
        str(data.get("question") or "").strip()[:200],
    )


class Listener:
    def __init__(self, ear: Calls) -> None:
        self._ear = ear

    async def hear(self, menu: dict[str, Any], pcm: bytes) -> Heard:
        import time

        started = time.perf_counter()
        try:
            reply, usd = await self._ear.call(request(menu, pcm))
        except ModelUnavailable as down:
            log.warning("could not hear: %s", down)
            return Heard("", understood=False)
        took = int((time.perf_counter() - started) * 1000)
        got = read(reply)
        if got is None:
            return Heard("", usd=usd, took_ms=took, understood=False)
        text, presses, question = got
        return Heard(text, presses, question, usd, took, bool(presses or question))
