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

A model-issued search takes priority over a partial transcript. A matching
lookup finishes into the tool cache. A different lookup is cancelled before
the authoritative search can change the shortlist.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.agent.tamil_numbers import digitise
from src.agent.tools.shortlist import search_key

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
    # Poorvika is not a phone shop. These were added after a call where a
    # customer asked for something off the phone shelf and the agent had no
    # way to look for it: the tool's category was an enum of five.
    #
    # `case` and `kitchen` are deliberately NOT here, though the tool can
    # still filter on them. A prefetch fires on a word plus a budget, and
    # "in that case" is a sentence, not a shelf.
    "ac": ("ac", "a/c", "aircon", "air conditioner", "ஏசி", "ஏ.சி.", "குளிரூட்டி"),
    "fridge": (
        "fridge",
        "refrigerator",
        "ஃப்ரிட்ஜ்",
        "குளிர்சாதனப் பெட்டி",
    ),
    "washing machine": (
        "washing machine",
        "washingmachine",
        "வாஷிங் மெஷின்",
        "வாஷிங்மெஷின்",
        "சலவையந்திரம்",
    ),
    "fan": ("fan", "ஃபேன்", "விசிறி"),
    "headphones": (
        "headphone",
        "headphones",
        "headset",
        "earphone",
        "earphones",
        "earbuds",
        "ஹெட்போன்",
        "இயர்போன்",
    ),
    "speaker": ("speaker", "speakers", "ஸ்பீக்கர்"),
    "powerbank": ("powerbank", "power bank", "பவர் பேங்க்", "பவர்பேங்க்"),
    "charger": ("charger", "சார்ஜர்"),
    "geyser": ("geyser", "water heater", "கீசர்", "வாட்டர் ஹீட்டர்"),
}

# A budget is only a budget above this. "5 phone paaren" is not five thousand
# rupees, and a bare digit or two is a count far more often than a price.
#
# Lowering it was tried, for the cheap shelves - a case is 300 rupees, a
# charger 600 - and put back, because this number does two jobs. `_rupees`
# reads a bare number BELOW the floor as thousands: "25 kulla" is 25,000,
# which is how people say it. Drop the floor to 500 and "2000 kulla" stops
# meaning twenty lakh (rejected by the ceiling, so no budget) and starts
# meaning two thousand - right for a power bank, wrong for the phone the
# same words usually mean, and the prefetch would hand the model a shortlist
# from the wrong price band without anyone noticing.
#
# So the cheap shelves simply do not prefetch. That costs one generation and
# nothing else: the model still searches and still answers correctly. A
# missed optimisation is not a bug; a confident answer about the wrong price
# band is.
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
# A ceiling on its own: "25k kulla", "under 25000", "25,000-ku keezha".
# வரை and வரைக்கும் are "up to" and came off the cascade's ear on a real
# transcript - the Live API's own transcription romanises, so they had not
# been seen before there was a second one.
_CEILING_WORDS = (
    r"kulla|kkulla|under|below|within|ulla|குள்ள|உள்ள|கீழ|keezha|ku keezha"
    r"|வரைக்கும்|வரை|varaikkum|varai"
)
# உள்ள is in that list and is also the ordinary word for "in" - "stock-ல
# உள்ள phone". It is safe only because it is never looked for on its own:
# every pattern below anchors it to a number immediately before it.
# The dative between the number and the ceiling: "25000-க்கு உள்ள", "25k-ku
# keezha".
#
# The `\b` is on the Latin branches only, and both halves of that are
# deliberate. It has to be there, or `ku` eats the start of "kulla" and then
# finds no ceiling word behind it. It must NOT be on க்கு: that word ends in
# the vowel sign ு, a combining mark, and a combining mark is not a `\w` - so
# `\b` after it asks for a boundary between two non-word characters and never
# matches. The same trap took a whole Tamil word apart in shadow.py.
_DATIVE = r"(?:[-\s]*(?:க்கு|(?:kku|ku)\b))?"
_CEILING_AFTER = re.compile(
    rf"(\d[\d,]*)\s*{_UNIT}\s*(?:rupees?|ரூபாய்|rs\.?)?{_DATIVE}"
    rf"\s*[-\s]*(?:{_CEILING_WORDS})",
    re.IGNORECASE,
)
# A floor on its own: "40k mela", "above 40000", "40,000-க்கு மேல".
#
# This used to return nothing, on the stated grounds that "20k mela is not
# one we can search on". That was wrong, and a trace says so: asked for a
# laptop above 40,000 the model called search_products with min_price and no
# max, found three, and the prefetch - which had refused to read the same
# sentence - sat out the turn. That silent generation was Rs 0.35 of a
# Rs 2.05 call, seventeen per cent, heard by nobody.
#
# மேல and "over" are ordinary words, so the same rule as உள்ள applies: they
# are only ever read with a number immediately in front of them.
_FLOOR_WORDS = r"mela|mேla|மேல|மேலே|above|over|plus|\+|aboveku|ku mela|kku mela"
_FLOOR_AFTER = re.compile(
    rf"(\d[\d,]*)\s*{_UNIT}\s*(?:rupees?|ரூபாய்|rs\.?)?{_DATIVE}"
    rf"\s*[-\s]*(?:{_FLOOR_WORDS})",
    re.IGNORECASE,
)
_FLOOR_BEFORE = re.compile(
    rf"(?:above|over|more than|minimum)\s*(\d[\d,]*)\s*{_UNIT}",
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
    max_price: int | None

    def as_args(self) -> dict[str, object]:
        """The same shape `search_products` would have been called with.

        Deliberately identical, so the prefetched answer is cached under the
        key the model's own call would produce and a call that arrives anyway
        is answered from it rather than going upstream twice.
        """
        args: dict[str, object] = {"category": self.category}
        if self.max_price is not None:
            args["max_price"] = self.max_price
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


def read_budget(text: str) -> tuple[int | None, int | None] | None:
    """A (floor, ceiling) the caller has said, or None if they said neither.

    Either end will do. "25k kulla" is a ceiling, "40k mela" is a floor, and
    `search_products` takes them one at a time - which is what the model
    itself does, so refusing to read one half was only ever costing a
    generation.

    Tamil number words are rewritten to digits first. The cascade's ear
    transcribes into Tamil script - "இருபதாயிரம் குள்ள" where the Live API
    writes "20000 kulla" - and every pattern below reads digit runs, so
    without that pass this returns None on every Tamil-script transcript
    there is. One rewrite in front keeps one set of rules behind it.
    """
    text = digitise(text)
    if match := _RANGE.search(text):
        low = _rupees(match.group(1), match.group(2) or match.group(4))
        high = _rupees(match.group(3), match.group(4))
        if low is not None and high is not None and low < high:
            return low, high
    for pattern in (_CEILING_AFTER, _CEILING_BEFORE):
        if match := pattern.search(text):
            if (high := _rupees(match.group(1), match.group(2))) is not None:
                return None, high
    # Ceilings first: "20k to 40k mela" is a range somebody trailed off, and
    # the range reading is the one they meant.
    for pattern in (_FLOOR_AFTER, _FLOOR_BEFORE):
        if match := pattern.search(text):
            if (low := _rupees(match.group(1), match.group(2))) is not None:
                return low, None
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
    if intent.max_price is None:
        band = (
            f"from {intent.min_price:,}"
            if intent.min_price is not None
            else "any price"
        )
    elif intent.min_price is None:
        band = f"up to {intent.max_price:,}"
    else:
        band = f"{intent.min_price:,} to {intent.max_price:,}"
    lines = [f"search_products has already run for {intent.category}, {band}:"]
    if isinstance(total := result.get("total_matching"), int):
        lines.append(f"total_matching={total}")
    if price_note := result.get("price_note"):
        lines.append(f"Prices: {price_note}.")
    for item in products:
        price = item.get("price_inr")
        line = f"{item.get('ref')}) {item.get('name')}"
        if isinstance(price, int):
            line += f", {price:,} rupees"
        # The specs are not decoration. persona.py requires one reason per
        # product "from the specs the tool returned", and a probe against the
        # live endpoint with a note that carried only names and prices got
        # "Realme P4 Lite 5G, 20,999 rupees - great camera" back. Nothing had
        # said anything about a camera. The search result carries this field
        # with a comment saying exactly that - "without it, asked why it
        # suggested something, it made a camera up" - and dropping it here
        # re-opened the bug that comment is about.
        specs = item.get("specs")
        if isinstance(specs, dict) and specs:
            line += " - " + ", ".join(f"{k} {v}" for k, v in specs.items())
        lines.append(line)
    lines.append("Do not call search_products again if these match the full request.")
    return "\n".join(lines)


class Recorder(Protocol):
    """`CallTrace.record`, named so this module does not import the trace."""

    def __call__(self, kind: str, **detail: Any) -> Any: ...


class Prefetcher:
    """Searches started early, while the caller is still talking.

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
        on_event: Recorder | None = None,
    ) -> None:
        self._tools = tools
        self._prime = prime
        self._timeout = timeout_s
        # Recorded as it happens rather than totalled at the end. Whether
        # this WON is the whole question - the model starts generating the
        # moment its own VAD says the caller stopped, and a prefetch that
        # primes a second later has paid for a search nobody used. "used:
        # true" at the end of a call does not say which.
        self._on_event = on_event
        # The last thing we prefetched. A prefetch only fires for an intent
        # that DIFFERS from this one.
        #
        # It used to be once a call, on the reasoning that "a second prefetch
        # would rebuild the shortlist under refs the customer has already been
        # told". The first half is true and the conclusion is not: when the
        # customer changes their budget the model searches again itself, and
        # `_search` only reuses a shortlist for a search with the SAME key, so
        # the refs move either way. The only thing the cap bought was that
        # they moved a generation later.
        #
        # Measured on the call of 2026-11-16: the customer moved from 30-40k
        # to 40k-up to 20-30k, three searches, six of the seven generations
        # in the call, and the prefetch fired for none of them.
        self._last: Intent | None = None
        self._claimed = False
        self._done = 0
        self.task: asyncio.Task[bool] | None = None
        self.fired = False
        self.used = False

    # A browsing customer changes their mind; they do not change it fifteen
    # times. The cap is here so that a transcript that keeps re-reading as a
    # new intent cannot sit in a loop asking the catalogue.
    MAX_PER_CALL = 3

    def heard(self, text: str) -> None:
        """A fragment of what the caller is saying. Returns immediately.

        Called from the adapter's receive loop, so it must not await: the next
        audio frame is behind it.
        """
        if self._done >= self.MAX_PER_CALL:
            return
        # One at a time. Two in flight would race to prime, and the shortlist
        # the model ends up holding would be whichever returned last.
        if self.task is not None and not self.task.done():
            return
        intent = read_intent(text) or self._same_shelf_new_price(text)
        if intent is None or intent == self._last:
            return
        self._last = intent
        self._claimed = False
        self._done += 1
        self._note("prefetch_started", **intent.as_args())
        self.task = asyncio.create_task(self._go(intent), name="prefetch")

    def _same_shelf_new_price(self, text: str) -> Intent | None:
        """A budget with no category, once a category is already on the table.

        "illa, 20-30k kulla paarunga" names no shelf, and a customer who has
        just been read three phones does not need to. `read_intent` requires
        both and is right to - with no history, a budget alone does not say
        which product. Here there IS history: the shelf is whatever we last
        searched, and only the price has moved.

        This is the half of the browsing case the measurement missed. The
        customer who moved from 30-40k to 40k-up to 20-30k said "phone" once.
        """
        if self._last is None:
            return None
        budget = read_budget(text)
        if budget is None:
            return None
        floor, ceiling = budget
        return Intent(category=self._last.category, min_price=floor, max_price=ceiling)

    async def settle(self, timeout_s: float) -> bool:
        """Wait for a prefetch in flight, briefly. True if it landed.

        This is what turns the race into a decision. The prefetch was always
        going to lose sometimes - it only starts when the caller has said
        enough to read an intent from, and on the trace of 2026-11-16 that
        was 740 ms before they stopped talking, against a search that needed
        longer. No amount of making the search faster creates head start
        that is not there.

        So the caller's turn is held instead, for at most this long, and the
        model is told they finished only once the shortlist is in its
        context. The wait is spent on exactly the turns where it saves a
        tool round trip, which is two generations and about three seconds -
        so it buys latency as well as money, not at its expense.

        Returns rather than raises on a timeout: a prefetch that is slow is
        a prefetch we stop waiting for, and the model searches as it always
        did.
        """
        task = self.task
        if task is None:
            return False
        # Cancellation of an obsolete lookup must not cancel the microphone
        # pump waiting here. Cancellation of the pump itself still propagates.
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        if not done:
            self._note("prefetch_waited_out", seconds=round(timeout_s, 2))
            return False
        return not task.cancelled() and task.exception() is None and task.result()

    async def before_search(self, args: dict[str, Any]) -> None:
        """Give the explicit tool call ownership of the result and its refs."""
        task = self.task
        if task is None or task.done() or self._last is None:
            return
        self._claimed = True
        if search_key(args) != search_key(self._last.as_args()):
            task.cancel()
        # A matching result will already be cached by ToolSession. For a
        # different search, drain cancellation before the new refs are set.
        await asyncio.gather(task, return_exceptions=True)

    def _note(self, kind: str, **detail: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, **detail)

    async def _go(self, intent: Intent) -> bool:
        try:
            result = await asyncio.wait_for(
                self._tools.search_ahead(intent.as_args()), self._timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001 - never the caller's problem
            log.warning("prefetch search failed: %s", type(failed).__name__)
            return False
        self.fired = True
        if result.get("error"):
            log.info("prefetch search returned %s", result["error"])
            return False
        if self._claimed or not (text := note(result, intent)):
            return False
        try:
            await self._prime(text)
        except Exception as failed:  # noqa: BLE001
            log.warning("prefetch could not be primed: %s", type(failed).__name__)
            return False
        self.used = True
        self._note("prefetch_primed", products=len(result.get("products") or []))
        log.info(
            "prefetched %s %s before the model asked",
            intent.category,
            intent.as_args(),
        )
        return True
