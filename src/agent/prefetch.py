"""What to search for, read off a sentence the caller has not finished.

A tool call costs two generations - one silent turn to ask and another to
answer - and every generation re-bills the whole context window. Across seven
measured calls, six of the nine silent generations were `search_products`.
That is the single largest remaining cost line, and it is also the slowest
moment on the call: first audio after a search was 3,095 ms against 595 ms
for an ordinary reply, because the search itself took 1,937 ms of it.

So the search is started while the caller is still talking, off the partial
input transcription, and the result is handed to the model as context. If it
is right, the model answers with no tool call at all. If it is wrong, the
model still has the tool and calls it.

## This is deliberately hard to trigger

It fires only when a category AND a budget are both already on the table.
Both, because a category alone ("phone irukka?") does not say which shelf and
a budget alone does not say which product. The whole value is in being right:
a prefetch that searched the wrong thing has spent an upstream round trip, put
a wrong shortlist in front of the model, and saved nothing.

Partial ASR is also not a transcript. It arrives in fragments, it revises
itself, and on a Tanglish call it romanises inconsistently. Everything here is
written to read a number and a noun out of a fragment and to return nothing at
all the moment it is unsure, which is most of the time.

## What it does NOT do

It does not touch an existing shortlist. Once the customer has been told about
"the first one" and "the second one", those refs point at phones they have
heard named, and rebuilding the shortlist under them is how an agent starts
answering about a different product than the one being discussed. So a
prefetch only happens before the first search of a call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

# What the catalogue actually has shelves for. The enum in the tool
# declaration is the same list, and it has to stay the same list: a prefetch
# for a category the model cannot name would be a search nobody can follow up.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "phone": ("phone", "mobile", "mobil", "smartphone", "கைபேசி", "போன்", "மொபைல்"),
    "laptop": ("laptop", "lapy", "notebook", "மடிக்கணினி", "லேப்டாப்"),
    "tablet": ("tablet", "tab", "ipad", "டேப்லெட்"),
    "watch": ("watch", "smartwatch", "கடிகாரம்", "வாட்ச்"),
    "tv": ("tv", "television", "டிவி", "தொலைக்காட்சி"),
}

# A budget is only a budget above this. "5 phone paaren" is not five thousand
# rupees, and a catalogue of phones has nothing under about this anyway.
FLOOR = 3000
# Above this it is being misheard. The estate's dearest phone is far below it.
CEILING = 500_000

# A unit must not be the first letter of the next word. Without this, "24,999
# kulla" read the k of kulla as thousands and asked for 24,999,000 rupees -
# which the ceiling then rejected, so a real budget became no budget at all.
_UNIT = r"(k|ஆயிரம்|thousand)?(?![a-z஀-௿])"
# What joins two ends of a range. "la irundhu" arrives glued to the first
# number with a hyphen - "20,000-la irundhu 25,000" - so the hyphen is part of
# the connector rather than a connector on its own.
_JOIN = r"(?:-?\s*(?:la\s+irundhu|ல\s*இருந்து|to|முதல்)|-|–)"
# "20-25k", "20000 to 25000", "20,000-la irundhu 25,000"
_RANGE = re.compile(
    rf"(\d[\d,]*)\s*{_UNIT}\s*{_JOIN}\s*(\d[\d,]*)\s*{_UNIT}",
    re.IGNORECASE,
)
# A ceiling on its own: "25k kulla", "under 25000", "25,000-ku keezha"
_CEILING_WORDS = r"kulla|kkulla|under|below|within|ulla|குள்ள|கீழ|keezha|ku keezha"
_CEILING_AFTER = re.compile(
    rf"(\d[\d,]*)\s*{_UNIT}\s*(?:rupees?|ரூபாய்|rs\.?)?\s*[-\s]*(?:{_CEILING_WORDS})",
    re.IGNORECASE,
)
_CEILING_BEFORE = re.compile(
    rf"(?:{_CEILING_WORDS})\s*(\d[\d,]*)\s*{_UNIT}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Intent:
    """A category and a budget, both of them, or this object does not exist."""

    category: str
    min_price: int | None
    max_price: int

    def as_args(self) -> dict[str, object]:
        """The same shape `search_products` would have been called with.

        Deliberately identical, so the prefetched answer is cached under the
        key the model's own call would produce and a call that arrives anyway
        is answered from it rather than going upstream twice.
        """
        args: dict[str, object] = {
            "category": self.category,
            "max_price": self.max_price,
        }
        if self.min_price is not None:
            args["min_price"] = self.min_price
        return args


def _rupees(digits: str, unit: str | None) -> int | None:
    """A spoken amount as rupees, or None when it is not an amount."""
    try:
        value = int(digits.replace(",", ""))
    except ValueError:
        return None
    if unit:
        value *= 1000
    # "20-25" with no unit, on a call about phones, means thousands. Below the
    # floor and with no unit it is a bare number, and a bare number that small
    # is a quantity, not a price.
    elif value < FLOOR:
        value *= 1000
    return value if FLOOR <= value <= CEILING else None


def _names(word: str, text: str) -> bool:
    """Whether `text` uses `word` as a word.

    `\\b` is no use on Tamil script: the pulli that ends மொபைல் is a combining
    mark, which is not a word character, so there is no boundary after it and
    `\\bமொபைல்\\b` matches nothing at all. ASCII words still need the boundary -
    without it "tab" matches inside "table" and a tablet search is a phone
    call about something else - so each word gets the test its script allows.
    """
    if word.isascii():
        return re.search(rf"\b{re.escape(word)}\b", text) is not None
    return word in text


def read_category(text: str) -> str | None:
    """The one shelf this sentence names, or None if it names none or two."""
    lowered = text.lower()
    found = {
        name
        for name, words in CATEGORIES.items()
        if any(_names(word, lowered) for word in words)
    }
    # Two shelves is not a narrower search, it is an ambiguous one.
    return found.pop() if len(found) == 1 else None


def read_budget(text: str) -> tuple[int | None, int] | None:
    """A (floor, ceiling) the caller has said, or None.

    A ceiling is required and a floor is not: "25k kulla" is a budget and
    "20k mela" is not one we can search on.
    """
    if match := _RANGE.search(text):
        low = _rupees(match.group(1), match.group(2) or match.group(4))
        high = _rupees(match.group(3), match.group(4))
        if low is not None and high is not None and low < high:
            return low, high
    for pattern in (_CEILING_AFTER, _CEILING_BEFORE):
        if match := pattern.search(text):
            if (high := _rupees(match.group(1), match.group(2))) is not None:
                return None, high
    return None


def read_intent(text: str) -> Intent | None:
    """Both halves, or nothing at all.

    Nothing at all is the expected answer. This is called on every fragment of
    partial transcription, and the fragment that carries a category and a
    budget together is the exception.
    """
    category = read_category(text)
    if category is None:
        return None
    budget = read_budget(text)
    if budget is None:
        return None
    floor, ceiling = budget
    return Intent(category=category, min_price=floor, max_price=ceiling)


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


class Searcher(Protocol):
    """The slice of `ToolSession` a prefetch needs."""

    async def search_ahead(self, args: dict[str, Any]) -> dict[str, Any]: ...

    @property
    def shortlist(self) -> tuple[Any, ...]: ...


# Puts text in the model's context without asking it to answer. Both the Live
# adapter and the rotating wrapper provide exactly this.
Primer = Callable[[str], Awaitable[None]]


def note(result: dict[str, Any], intent: Intent) -> str:
    """What to tell the model, composed from the result and nothing else.

    Same discipline as `voice/handover.py`: every price and name here came
    back from the catalogue, so the note cannot put a number in the agent's
    mouth that no tool returned.

    The last line is the whole point of the exercise. Without it the model
    reads a shortlist and calls search_products anyway, and the generation
    this was built to avoid is spent after all.
    """
    products = result.get("products") or []
    if not products:
        return ""
    band = f"up to {intent.max_price:,}"
    if intent.min_price is not None:
        band = f"{intent.min_price:,} to {intent.max_price:,}"
    lines = [f"search_products has already run for {intent.category}, {band}:"]
    for item in products:
        price = item.get("price_inr")
        lines.append(
            f"{item.get('ref')}) {item.get('name')}"
            + (f", {price:,} rupees" if isinstance(price, int) else "")
        )
    lines.append("Answer from these. Do not call search_products for this.")
    return "\n".join(lines)


class Prefetcher:
    """One search, started early, at most once a call.

    Everything here is written so that failing is free. A prefetch that
    errors, times out, or turns out to be about the wrong thing leaves the
    call exactly as it was: the tool is still declared, the model still calls
    it, and the customer never learns that anything was attempted.
    """

    def __init__(
        self,
        tools: Searcher,
        prime: Primer,
        *,
        timeout_s: float = 6.0,
    ) -> None:
        self._tools = tools
        self._prime = prime
        self._timeout = timeout_s
        # At most once per call. A second prefetch would rebuild the shortlist
        # under refs the customer has already been told, which is the one
        # outcome worse than not prefetching at all.
        self._started = False
        self.task: asyncio.Task[None] | None = None
        self.fired = False
        self.used = False

    def heard(self, text: str) -> None:
        """A fragment of what the caller is saying. Returns immediately.

        Called from the adapter's receive loop, so it must not await: the next
        audio frame is behind it.
        """
        if self._started or self._tools.shortlist:
            return
        intent = read_intent(text)
        if intent is None:
            return
        self._started = True
        self.task = asyncio.create_task(self._go(intent), name="prefetch")

    async def _go(self, intent: Intent) -> None:
        try:
            result = await asyncio.wait_for(
                self._tools.search_ahead(intent.as_args()), self._timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001 - never the caller's problem
            log.warning("prefetch search failed: %s", type(failed).__name__)
            return
        self.fired = True
        if result.get("error"):
            log.info("prefetch search returned %s", result["error"])
            return
        if not (text := note(result, intent)):
            return
        try:
            await self._prime(text)
        except Exception as failed:  # noqa: BLE001
            log.warning("prefetch could not be primed: %s", type(failed).__name__)
            return
        self.used = True
        log.info(
            "prefetched %s %s before the model asked",
            intent.category,
            intent.as_args(),
        )
