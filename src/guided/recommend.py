"""Five to show, and why each one. The only place a model may be asked.

Three ways to arrive at the five, tried in the order that costs least:

1. **Five or fewer are left.** There is nothing to choose between, so
   nothing is asked of anyone. They are put in a sensible order and shown.
2. **These answers have been seen before, on this shelf as it stands.** The
   earlier answer is returned. A hundred customers who all want a camera
   phone under 30,000 are one think, not a hundred.
3. **The model.** Once, and only if the governor says the session has
   earned it. Otherwise - model down, budget refused, reply unusable - the
   order below is the answer, and the customer still gets five products.

So a recommendation never fails for want of a model, and the worst a
time-waster can cost is one think per distinct set of answers per shelf.

## What the model is not trusted with

It returns ids and a sentence. Ids outside the list it was given are
dropped. A sentence is kept only if every figure in it is a figure on that
product's own card, it is in Tamil, and it names no price - otherwise the
sentence built here from the product's own facts is used instead. The first
run of this, on the old data, said "nalla camera irukku" of every phone;
the check does not make the model better, it makes its worst case a true
sentence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from src.guided.engine import CONFIRM, SHOW, Answers, Ready
from src.guided.listen import in_language
from src.guided.script import PRIORITY, word
from src.guided.script import TAMIL
from src.guided.script import Priority, Script, rupees
from src.guided.shelf import Item
from src.guided.thinker import Brief, Thinker, ThinkerUnavailable

log = logging.getLogger(__name__)

# How many the model is shown. Twelve is where the measured prompt stayed
# near a thousand tokens; past it the cost rises and the ranking does not.
CANDIDATES = 12
# How many distinct recommendations are remembered. Each is five short
# sentences, so this is kilobytes.
REMEMBERED = 512
WHY_CHARS = 140

# How a recommendation was arrived at. Shown in the developer drawer, and
# the difference between "free because cached" and "free because refused"
# is the first thing anyone debugging a bill wants to know.
ALL, CACHE, MODEL, RULES = "all", "cache", "model", "rules"

# The card line a measure is shown under, so a reason can quote it.
_SHOWN_AS = {
    "camera_mp": "கேமரா",
    "battery_mah": "பேட்டரி",
    "ram_gb": "RAM",
    "storage_gb": "ஸ்டோரேஜ்",
    "stabilisation": "Video stabilisation",
    "hdmi_arc": "HDMI ARC",
    "bluetooth": "Bluetooth",
    "portable": "Battery powered",
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TAMIL = re.compile(r"[஀-௿]")
_MONEY = re.compile(r"₹|\brs\b\.?|rupee|ரூ", re.IGNORECASE)
_LATIN = re.compile(r"[a-zA-Z]")


@dataclass(frozen=True)
class Pick:
    ref: str
    item: Item
    why: str
    # True when this one sits a little over the budget the customer chose.
    # Only ever shown flagged: "it is a little over what you said" is a
    # sentence a salesperson says out loud, not a thing to slip past anyone.
    over: bool = False


@dataclass(frozen=True)
class Recommendation:
    picks: tuple[Pick, ...]
    source: str
    usd: float = 0.0
    took_ms: int = 0
    tokens: int = 0


def _priority(script: Script, chosen: str | None) -> Priority | None:
    return next((p for p in script.priorities if p.id == chosen), None)


def ranked(items: tuple[Item, ...], priority: Priority | None) -> list[Item]:
    """The order to show them in when nobody is asked. Deterministic.

    What the customer said matters most comes first. After that: better
    rated, then stocked in more branches - which is the nearest thing here
    to "what people are actually buying" and also means it is near them -
    then cheaper. A priority with nothing to measure ("value for money")
    puts the largest saving first.
    """

    def saving(item: Item) -> float:
        return (item.was - item.price) / item.was if item.was else 0.0

    def order(item: Item) -> tuple[float, float, float, float, int, str]:
        if priority is None:
            first = 0.0
        elif priority.measure is None:
            first = -saving(item)
        else:
            first = -item.measures.get(priority.measure, -1.0)
        return (
            first,
            -(item.rating or 0.0),
            -float(item.branches or 0),
            -saving(item),
            item.price,
            item.name,
        )

    return sorted(items, key=order)


def reason(item: Item, priority: Priority | None, lang: str = TAMIL) -> str:
    """One line built from the product's own facts. Never empty of truth.

    This is what is shown when no model was asked, and what replaces a
    model's sentence that failed the check - so it must be something that
    can be said of this product without anyone having judged it.
    """
    facts = dict(item.specs)
    parts: list[str] = []
    if priority and priority.measure in {
        "stabilisation",
        "hdmi_arc",
        "bluetooth",
        "portable",
    }:
        name = word(priority.words, lang)
        if item.measures.get(priority.measure) == 1:
            return f"{name} — " + (
                "verified for this model"
                if lang == "en"
                else "இந்த மாடலில் உறுதி செய்யப்பட்டது"
            )
        if item.measures.get(priority.measure) == 0:
            return f"{name} — " + (
                "unavailable on this model" if lang == "en" else "இந்த மாடலில் இல்லை"
            )
        return f"{name} — " + (
            "unconfirmed for this model"
            if lang == "en"
            else "இந்த மாடலில் உறுதி செய்ய முடியவில்லை"
        )
    wanted = _SHOWN_AS.get(priority.measure) if priority and priority.measure else None
    if wanted and wanted in facts:
        parts.append(f"{wanted} {facts[wanted]}")
    for label, value in item.specs:
        if len(parts) >= 2:
            break
        if label != wanted:
            parts.append(f"{label} {value}")
    if item.was:
        saved = rupees(item.was - item.price)
        parts.append(f"{saved} off" if lang == "en" else f"{saved} " + "குறைவு")
    if lang == "en":
        labels = {
            "கேமரா": "Camera",
            "பேட்டரி": "Battery",
            "ஸ்டோரேஜ்": "Storage",
            "டிஸ்ப்ளே": "Display",
            "நெட்வொர்க்": "Network",
            "சேனல்": "Channels",
        }
        parts = [
            next(
                (
                    en + part[len(ta) :]
                    for ta, en in labels.items()
                    if part.startswith(ta)
                ),
                part,
            )
            for part in parts
        ]
    return " · ".join(parts)


def _figures(text: str) -> set[float]:
    return {float(number) for number in _NUMBER.findall(text.replace(",", ""))}


def sound(why: str, item: Item, lang: str = TAMIL) -> bool:
    """May this sentence be shown beside this product?

    Every figure in it must be one the product's own name or card states. A
    model that writes "108MP" of a phone listed at 50MP has not been
    creative, it has been wrong in the customer's hand.
    """
    if not why or len(why) > WHY_CHARS:
        return False
    if not in_language(why, lang) or _MONEY.search(why):
        return False
    known = _figures(item.name + " " + " ".join(value for _, value in item.specs))
    return _figures(why) <= known


def _wants(script: Script, answers: Answers, lang: str) -> tuple[tuple[str, str], ...]:
    """What the shopper chose, said plainly for the model.

    Labels come off the buttons, and one button does not say what it means:
    accepting the suggested spec reads "சரி, அப்படியே பாரு", which tells a
    model nothing. That answer is restated as the priority it accepted.
    """
    by_id = {p.id: p for p in script.priorities}
    return tuple(
        (
            given.question,
            word(by_id[given.choice].words, lang)
            if given.question == PRIORITY and given.choice in by_id
            else given.label,
        )
        for given in answers
        # The read-back is a gate, not a want: "yes, that is right" tells
        # the model nothing the answers above it do not.
        if given.question != CONFIRM
    )


def _key(script: Script, answers: Answers, candidates: list[Item], lang: str) -> str:
    """These answers, on this shelf as it stands. Stock or price moving changes it."""
    stated = [
        script.shelf,
        lang,
        [(given.question, given.choice) for given in answers],
        [
            (
                item.item_code,
                item.name,
                item.price,
                item.was,
                item.specs,
                item.measures,
                item.stock_confirmed,
                item.branches,
                item.rating,
            )
            for item in candidates
        ],
    ]
    return hashlib.sha256(json.dumps(stated, ensure_ascii=False).encode()).hexdigest()


class Recommender:
    def __init__(
        self, thinker: Thinker | None = None, remembered: int = REMEMBERED
    ) -> None:
        self._thinker = thinker
        self._remembered = remembered
        self._seen: OrderedDict[str, tuple[Pick, ...]] = OrderedDict()

    def _by_rule(
        self, order: list[Item], priority: Priority | None, lang: str = TAMIL
    ) -> tuple[Pick, ...]:
        return tuple(
            Pick(str(number), item, reason(item, priority, lang))
            for number, item in enumerate(order[:SHOW], start=1)
        )

    @staticmethod
    def _filled(
        picks: tuple[Pick, ...], ready: Ready, priority: Priority | None, lang: str
    ) -> tuple[Pick, ...]:
        """A short list topped up from just over the budget, flagged.

        A salesperson with two phones in the band says "and this one is a
        little over what you said" about a third - and SAYS it, which is
        what `over` makes the page do.
        """
        out = list(picks)
        for item in ranked(ready.stretch, priority):
            if len(out) >= SHOW:
                break
            out.append(
                Pick(str(len(out) + 1), item, reason(item, priority, lang), over=True)
            )
        return tuple(out)

    async def choose(
        self,
        script: Script,
        answers: Answers,
        ready: Ready,
        may_spend: Callable[[], bool],
        lang: str = TAMIL,
    ) -> Recommendation:
        """The five. `may_spend` is asked at most once, and only if needed."""
        priority = _priority(script, ready.priority)
        order = ranked(ready.items, priority)
        if len(order) <= SHOW:
            picks = self._filled(
                self._by_rule(order, priority, lang), ready, priority, lang
            )
            return Recommendation(picks, ALL)

        candidates = order[:CANDIDATES]
        key = _key(script, answers, candidates, lang)
        if (seen := self._seen.get(key)) is not None:
            self._seen.move_to_end(key)
            return Recommendation(seen, CACHE)

        if (
            priority
            and priority.measure
            and len({i.measures.get(priority.measure) for i in candidates}) > 1
        ):
            return Recommendation(self._by_rule(order, priority, lang), RULES)
        if self._thinker is None or not may_spend():
            return Recommendation(self._by_rule(order, priority, lang), RULES)

        brief = Brief(
            script.shelf,
            _wants(script, answers, lang),
            tuple(candidates),
            lang,
        )
        try:
            thought = await self._thinker.rank(brief)
        except ThinkerUnavailable as down:
            log.warning("the think was not answered: %s", down)
            return Recommendation(self._by_rule(order, priority, lang), RULES)

        picks = self._checked(thought.picks, candidates, order, priority, lang)
        tokens = (
            thought.prompt_tokens + thought.response_tokens + thought.thinking_tokens
        )
        if not thought.picks:
            # Paid for and unusable. Charged, said so, and not remembered.
            return Recommendation(picks, RULES, thought.usd, thought.took_ms, tokens)
        self._seen[key] = picks
        while len(self._seen) > self._remembered:
            self._seen.popitem(last=False)
        return Recommendation(picks, MODEL, thought.usd, thought.took_ms, tokens)

    @staticmethod
    def _checked(
        answered: tuple[tuple[str, str], ...],
        candidates: list[Item],
        order: list[Item],
        priority: Priority | None,
        lang: str = TAMIL,
    ) -> tuple[Pick, ...]:
        """The model's picks, minus anything it had no business saying."""
        by_number = {str(n): item for n, item in enumerate(candidates, start=1)}
        chosen: list[tuple[Item, str]] = []
        for number, why in answered:
            item = by_number.pop(number, None)
            if item is None:
                # Not in the list, or named twice. Either way not a pick.
                continue
            chosen.append((item, reason(item, priority, lang)))
            if len(chosen) == SHOW:
                break
        # Fewer than five usable picks is topped up in our own order, so a
        # reply that was half right still shows five products.
        taken = {item.item_code for item, _ in chosen}
        for item in order:
            if len(chosen) == SHOW:
                break
            if item.item_code not in taken:
                chosen.append((item, reason(item, priority, lang)))
        return tuple(
            Pick(str(number), item, why)
            for number, (item, why) in enumerate(chosen, start=1)
        )
