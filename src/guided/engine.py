"""Which question comes next. A pure function of the shelf and the answers.

No I/O, no clock, no model. Everything here can be run a million times for
nothing, which is what lets the questions be free: the only thing a tap does
is narrow a list that was already in memory.

The order is a salesperson's: brand first, then the budget, then what the
thing is FOR - with the follow-up a salesperson actually asks, "studying
what?", "what kind of work?" - and then the suggestion that falls out of it,
put as a question the customer may overrule. A question that narrows is
asked only while more than five products remain and it would split them;
the purpose questions never narrow, so they are asked while there is still
a real choice to inform, and skipped once five or fewer remain - at five,
the customer sees everything there is either way.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from src.guided.script import (
    ACCEPT_SPEC,
    FEATURE_WORDS,
    CONFIRM_CHANGE,
    CONFIRM_YES,
    ANY,
    ANY_BUDGET,
    BRAND,
    BUDGET,
    BUDGET_UP_NO,
    BUDGET_UP_YES,
    PRIORITY,
    PURPOSE,
    TAMIL,
    USE,
    Choice,
    Facet,
    Script,
    Use,
    line,
    rupees,
    word,
)
from src.guided.shelf import Item

# How many products a customer is shown at a time - "my top four", with
# "show me more" walking the rest in fours. Also where the questions stop.
SHOW = 4
# The most buttons one question may carry, before "any". A phone line reads
# these out against number keys, and there are nine of those.
MOST_CHOICES = 8
# Keypad phones are left out unless the ceiling is at or under this: above
# it a customer is choosing between smartphones, and a ₹1,399 keypad phone
# is not an answer to "I shoot video on site" however the ranking falls.
KEYPAD_BUDGET = 5_000
# How far past a chosen budget a product may be and still be worth showing,
# flagged, when the band itself runs short. A salesperson says "it is a
# little over what you said" about this much; more than this is not "a
# little" and is not shown.
STRETCH = 0.15

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# "What is it for?" and its follow-ups, as deep as a branch of the tree
# goes: use -> "IT" -> "developer or testing".
USE_LEVELS = (USE, "use2")
# Whether a thin budget band may be topped up from just above it. Asked,
# never assumed: "it is a little over what you said" is the owner's own
# script, and the customer answers it before anything over is shown.
BUDGET_UP = "budget_up"
UP_YES, UP_NO = "yes", "no"
# Answers that are not ladder questions. A FEATURE is a must ("USB-C
# வேணும்"), kept only where the PIM sheet says yes; EXCLUDE takes one brand
# off ("Samsung தவிர"); PREFER puts one first without hiding the rest.
FEATURE, EXCLUDE, PREFER = "feature", "exclude", "prefer"
# The closing gate: the need is read back in one line, and nothing is
# recommended until the customer says it is right.
CONFIRM = "confirm"

# The questions that inform the pick without narrowing the list.
_SOFT = frozenset(
    {"consider", PURPOSE, PRIORITY, BUDGET_UP, CONFIRM, PREFER, *USE_LEVELS}
)


class NotOnOffer(ValueError):
    """An answer to a question that is not being asked, or not among its buttons.

    Every answer is checked against the buttons the engine would draw right
    now. Nothing a client sends is ever used as a filter value directly, so
    there is no value to inject: the worst a forged request can do is name a
    button that exists.
    """


@dataclass(frozen=True)
class Answer:
    question: str
    choice: str
    # What was on the button, kept so the page can show what was chosen
    # without anything being worked out twice.
    label: str


Answers = tuple[Answer, ...]


@dataclass(frozen=True)
class Band:
    """One budget button. `low` is inside the band and `high` is not."""

    id: str
    low: int | None
    high: int | None
    label: str

    def holds(self, price: int) -> bool:
        return (self.low is None or price >= self.low) and (
            self.high is None or price < self.high
        )


@dataclass(frozen=True)
class Ask:
    """A question to put, and the only answers that will be accepted.

    `note` is the sentence under the question, when the question is a
    suggestion - "for engineering: speed and 8GB RAM". The ask itself stays
    a fixed, recordable line; the note is shown, not spoken.
    """

    question: str
    ask: str
    choices: tuple[Choice, ...]
    note: str = ""


@dataclass(frozen=True)
class Ready:
    """Nothing left to ask.

    `items` is what the answers leave on the shelf. `stretch` is what sits
    just over the chosen budget - shown only to fill out a short list, and
    always flagged as over.
    """

    items: tuple[Item, ...]
    priority: str | None
    stretch: tuple[Item, ...] = ()


def bands(script: Script, lang: str = TAMIL) -> tuple[Band, ...]:
    """The script's budget ladder as bands, open at both ends."""
    edges = script.edges
    if lang == "en":
        first, last = f"Under {rupees(edges[0])}", f"Over {rupees(edges[-1])}"
    else:
        first, last = f"{rupees(edges[0])}-க்குள்", f"{rupees(edges[-1])}-க்கு மேல்"
    out = [Band(f"0-{edges[0]}", None, edges[0], first)]
    for low, high in zip(edges, edges[1:]):
        out.append(Band(f"{low}-{high}", low, high, f"{rupees(low)} – {rupees(high)}"))
    out.append(Band(f"{edges[-1]}-", edges[-1], None, last))
    return tuple(out)


def exact_budget(choice: str, lang: str = TAMIL) -> Band | None:
    """A spoken or typed customer limit, inclusive at either end."""
    match = re.fullmatch(
        r"(cap|floor|range):([1-9][0-9]{0,8})(?:-([1-9][0-9]{0,8}))?", choice
    )
    if not match:
        return None
    kind, first, second = match.groups()
    amount = int(first)
    if kind == "range":
        if not second or int(second) < amount:
            return None
        return Band(
            choice, amount, int(second) + 1, f"{rupees(amount)} – {rupees(int(second))}"
        )
    if second:
        return None
    if kind == "floor":
        label = f"From {rupees(amount)}" if lang == "en" else f"{rupees(amount)} முதல்"
        return Band(choice, amount, None, label)
    label = f"Up to {rupees(amount)}" if lang == "en" else f"{rupees(amount)} வரை"
    return Band(choice, None, amount + 1, label)


def _chosen_band(script: Script, answers: Answers) -> Band | None:
    by_id = {band.id: band for band in bands(script)}
    for given in answers:
        if given.question == BUDGET and given.choice != ANY:
            return by_id.get(given.choice) or exact_budget(given.choice)
    return None


def matching(
    script: Script, items: tuple[Item, ...], answers: Answers
) -> tuple[Item, ...]:
    """What is left of the shelf after these answers."""
    left = items
    if not _keypad_budget(script, answers):
        left = tuple(i for i in left if i.attributes.get("kind") != "keypad")
    for given in answers:
        if given.choice == ANY or given.question in _SOFT:
            continue
        if given.question == BUDGET:
            band = _chosen_band(script, answers)
            left = tuple(i for i in left if band is not None and band.holds(i.price))
        elif given.question == FEATURE:
            left = tuple(i for i in left if i.measures.get(given.choice) == 1.0)
        elif given.question == EXCLUDE:
            off = given.choice.lower()
            left = tuple(
                i for i in left if str(i.attributes.get(BRAND, "")).lower() != off
            )
        else:
            left = tuple(
                i for i in left if i.attributes.get(given.question) == given.choice
            )
    return left


def _keypad_budget(script: Script, answers: Answers) -> bool:
    """A ceiling only keypad phones fit under."""
    band = _chosen_band(script, answers)
    return band is not None and band.high is not None and band.high <= KEYPAD_BUDGET + 1


def stretched(
    script: Script, items: tuple[Item, ...], answers: Answers
) -> tuple[Item, ...]:
    """What sits just over the chosen budget, every other answer honoured.

    Empty when no bounded budget was chosen: with no ceiling there is
    nothing to be a little over.
    """
    band = _chosen_band(script, answers)
    if band is None or band.high is None or exact_budget(band.id):
        return ()
    without_budget = tuple(g for g in answers if g.question != BUDGET)
    near = matching(script, items, without_budget)
    limit = band.high * (1 + STRETCH)
    return tuple(
        sorted(
            (i for i in near if band.high <= i.price <= limit), key=lambda i: i.price
        )
    )


def _budget_choices(
    script: Script, left: tuple[Item, ...], lang: str
) -> tuple[Choice, ...]:
    return tuple(
        Choice(band.id, band.label, count)
        for band in bands(script, lang)
        if (count := sum(1 for item in left if band.holds(item.price)))
    )


def _natural(label: str) -> tuple[float, str]:
    """32 inch before 43 before 55, and words after numbers."""
    numbers = _NUMBER.findall(label)
    return (float(numbers[0]) if numbers else float("inf"), label)


def _facet_choices(facet: Facet, left: tuple[Item, ...]) -> tuple[Choice, ...]:
    counted = Counter(
        value for item in left if (value := item.attributes.get(facet.id))
    )
    commonest = sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))
    kept = commonest[:MOST_CHOICES]
    if facet.id != BRAND:
        # Sizes and capacities read in order. Brands read by how many there
        # are, which is the order a shop would name them in.
        kept = sorted(kept, key=lambda pair: _natural(pair[0]))
    return tuple(Choice(value, value, count) for value, count in kept)


def chosen_use(script: Script, answers: Answers) -> Use | None:
    """The use the customer named, at its most specific."""
    by_question = {given.question: given.choice for given in answers}
    node = next((u for u in script.uses if u.id == by_question.get(PURPOSE)), None)
    if node is None:
        return None
    for level in USE_LEVELS:
        deeper = next((u for u in node.then if u.id == by_question.get(level)), None)
        if deeper is None:
            break
        node = deeper
    return node


def _spec_choices(script: Script, suggested: str, lang: str) -> tuple[Choice, ...]:
    """Accept the suggestion first, then every other thing that could matter."""
    accept = Choice(suggested, word(ACCEPT_SPEC, lang))
    others = tuple(
        Choice(p.id, word(p.words, lang))
        for p in script.priorities
        if p.id != suggested
    )
    return (accept, *others)


def next_step(
    script: Script, items: tuple[Item, ...], answers: Answers, lang: str = TAMIL
) -> Ask | Ready:
    """The question to ask now, or `Ready` when there is none worth asking."""
    left = matching(script, items, answers)
    asked = {given.question for given in answers}

    if left:
        # Every question is asked, however few remain. The first build
        # skipped the ladder once four or fewer were left, and the owner
        # walked into it on the speaker shelf: brand tapped, products
        # thrown at him - "அப்படி பண்ணக்கூடாது. step by step refine பண்ணி
        # கடைசியா தான் product எடுத்து கொடுக்கணும்." The questions are
        # free; the shortcut saved nothing and sold the consultation short.
        # Brand (and size, and type) before budget, as he asks them.
        for facet in script.facets:
            if facet.id in asked:
                continue
            if len(choices := _facet_choices(facet, left)) > 1:
                return Ask(
                    facet.id,
                    line(facet.ask, lang),
                    (*choices, Choice(ANY, word(facet.any_words, lang), len(left))),
                )
        if BUDGET not in asked and (choices := _budget_choices(script, left, lang)):
            return Ask(
                BUDGET,
                line("ask_budget", lang),
                (*choices, Choice(ANY, word(ANY_BUDGET, lang), len(left))),
            )
        if script.uses:
            if PURPOSE not in asked:
                return Ask(
                    PURPOSE,
                    line(script.ask_purpose, lang),
                    tuple(Choice(u.id, word(u.words, lang)) for u in script.uses),
                )
            use = chosen_use(script, answers)
            considered = next(
                (a.choice.split(",") for a in answers if a.question == "consider"), []
            )
            if considered and PRIORITY not in asked:
                return Ask(
                    PRIORITY,
                    "Which matters most?"
                    if lang == "en"
                    else "இதுல உங்களுக்கு எது முக்கியம்?",
                    tuple(
                        Choice(p.id, word(p.words, lang))
                        for p in script.priorities
                        if p.id in considered
                    ),
                )
            if use is not None and use.then and PRIORITY not in asked:
                # The next unanswered level of the tree: "what work?", then
                # "what kind of IT work?" - each a recorded line of its own.
                level = USE_LEVELS[min(_depth(script, answers), len(USE_LEVELS) - 1)]
                return Ask(
                    level,
                    line(use.ask_next, lang),
                    tuple(Choice(u.id, word(u.words, lang)) for u in use.then),
                )
            if (
                use is not None
                and not use.suggests
                and PRIORITY not in asked
                and len(script.priorities) > 1
            ):
                # A course or a job title is not a spec. The owner's brief:
                # "நான் engineer" must not become "gaming, more RAM, a dearer
                # phone" - so ask what matters, with nothing picked for them.
                return Ask(
                    PRIORITY,
                    line("ask_priority", lang),
                    tuple(Choice(p.id, word(p.words, lang)) for p in script.priorities),
                )
            if use is not None and PRIORITY not in asked and len(script.priorities) > 1:
                # The suggestion, put as a question. The customer's "no,
                # camera matters more" is just another button.
                return Ask(
                    PRIORITY,
                    line("ask_spec", lang),
                    _spec_choices(script, use.suggests, lang),
                    note=word(use.spec, lang),
                )
        elif PRIORITY not in asked and len(script.priorities) > 1:
            return Ask(
                PRIORITY,
                line("ask_priority", lang),
                tuple(Choice(p.id, word(p.words, lang)) for p in script.priorities),
            )

    if (left or stretched(script, items, answers)) and CONFIRM not in asked:
        return Ask(
            CONFIRM,
            line("ask_confirm", lang),
            (
                Choice("yes", word(CONFIRM_YES, lang)),
                Choice("change", word(CONFIRM_CHANGE, lang)),
            ),
            note=summary(script, answers, lang),
        )

    over = stretched(script, items, answers) if len(left) < SHOW else ()
    if over and BUDGET_UP not in asked:
        # The owner's own moment: "the right one is a little over what you
        # said - shall I show it?" Asked out loud, answered with a button,
        # and nothing above the budget is shown until it is.
        return Ask(
            BUDGET_UP,
            line("ask_budget_up", lang),
            (
                Choice(UP_YES, word(BUDGET_UP_YES, lang), len(over)),
                Choice(UP_NO, word(BUDGET_UP_NO, lang), len(left)),
            ),
        )
    agreed = any(g.question == BUDGET_UP and g.choice == UP_YES for g in answers)

    chosen = next((g.choice for g in answers if g.question == PRIORITY), None)
    if chosen is None and (use := chosen_use(script, answers)) is not None:
        # They said what it is for and were never asked the spec question
        # (the list ran short): the suggestion still orders the answer.
        chosen = use.suggests or None
    return Ready(
        left,
        chosen if chosen != ANY else None,
        over if agreed else (),
    )


def extras(answers: Answers, lang: str = TAMIL) -> list[str]:
    """Features, exclusions and preferences, said the way the summary says
    them: "USB-C வேணும்", "Samsung தவிர", "Vivo இருந்தா நல்லது"."""
    said: list[str] = []
    for given in answers:
        if given.question == FEATURE:
            name = word(
                FEATURE_WORDS.get(given.choice, (given.choice, given.choice)), lang
            )
            said.append(f"{name} வேணும்" if lang == TAMIL else f"with {name}")
        elif given.question == EXCLUDE:
            said.append(
                f"{given.label} தவிர" if lang == TAMIL else f"not {given.label}"
            )
        elif given.question == PREFER:
            said.append(
                f"{given.label} இருந்தா நல்லது"
                if lang == TAMIL
                else f"{given.label} preferred"
            )
    return said


def brief(script: Script, answers: Answers, lang: str = TAMIL) -> dict[str, Any]:
    """The shopping brief, kept apart the way the owner's spec asks: what
    MUST hold, what is PREFERRED, what matters most, and what is still not
    known. The read-back says it in words; this says it to the app."""
    chosen = {given.question: given for given in answers}
    must: dict[str, Any] = {}
    prefer: dict[str, Any] = {}
    if (budget := chosen.get(BUDGET)) and budget.choice != ANY:
        must["budget"] = budget.label
    if (brand := chosen.get(BRAND)) and brand.choice != ANY:
        must["brand"] = brand.label
    features = [g.choice for g in answers if g.question == FEATURE]
    if features:
        must["features"] = features
    excluded = [g.label for g in answers if g.question == EXCLUDE]
    if excluded:
        must["not_brands"] = excluded
    preferred = [g.label for g in answers if g.question == PREFER]
    if preferred:
        prefer["brands"] = preferred
    use = chosen_use(script, answers)
    if use is not None:
        prefer["use"] = word(use.words, lang)
    priority = chosen.get(PRIORITY)
    named = [p.id for p in script.priorities if priority and p.id == priority.choice]
    if not named and use is not None and use.suggests:
        named = [use.suggests]
    unknown = [q for q in (BRAND, BUDGET, PURPOSE, PRIORITY) if q not in chosen]
    return {"must": must, "prefer": prefer, "priority": named, "unknown": unknown}


def relaxations(
    script: Script, items: tuple[Item, ...], answers: Answers, lang: str = TAMIL
) -> list[Choice]:
    """When the cards run out: each ONE change that would bring more, and
    how many more. The owner's spec - ask which condition to change - with
    honest counts, and only changes that actually find something."""
    now = len(matching(script, items, answers))
    offers: list[Choice] = []

    def gained(changed: Answers) -> int:
        return len(matching(script, items, changed)) - now

    band = _chosen_band(script, answers)
    if band is not None and band.high is not None:
        higher = next((e for e in script.edges if e > band.high), None)
        ceiling = higher or int(band.high * 1.25)
        raised = tuple(
            Answer(BUDGET, f"cap:{ceiling}", rupees(ceiling))
            if g.question == BUDGET
            else g
            for g in answers
        )
        if (more := gained(raised)) > 0:
            label = (
                f"{rupees(ceiling)} வரை பாக்கலாம் (+{more})"
                if lang == TAMIL
                else f"Look up to {rupees(ceiling)} (+{more})"
            )
            offers.append(Choice(f"budget:{ceiling}", label, more))
    for given in answers:
        if given.question == BRAND and given.choice != ANY:
            loosened = tuple(
                Answer(BRAND, ANY, "") if g is given else g for g in answers
            )
            if (more := gained(loosened)) > 0:
                label = (
                    f"எந்த பிராண்டும் பாக்கலாம் (+{more})"
                    if lang == TAMIL
                    else f"Any brand (+{more})"
                )
                offers.append(Choice("brand", label, more))
        elif given.question in (FEATURE, EXCLUDE):
            dropped = tuple(g for g in answers if g is not given)
            if (more := gained(dropped)) > 0:
                if given.question == FEATURE:
                    name = word(
                        FEATURE_WORDS.get(given.choice, (given.choice, given.choice)),
                        lang,
                    )
                    label = (
                        f"{name} இல்லாமலும் பாக்கலாம் (+{more})"
                        if lang == TAMIL
                        else f"Without {name} (+{more})"
                    )
                else:
                    label = (
                        f"{given.label}-ஐயும் பாக்கலாம் (+{more})"
                        if lang == TAMIL
                        else f"Include {given.label} too (+{more})"
                    )
                offers.append(Choice(f"{given.question}:{given.choice}", label, more))
    return offers


def summary(script: Script, answers: Answers, lang: str = TAMIL) -> str:
    """The need in one line, from the customer's own answers.

    "₹20,000 - ₹30,000, Samsung, காலேஜ் - ஒரு போன். கேமரா முக்கியம்."
    Composed only from the labels that were on the buttons they chose, so
    it can never claim something they did not say.
    """
    chosen = {given.question: given for given in answers}
    parts: list[str] = []
    if (budget := chosen.get(BUDGET)) and budget.choice != ANY:
        parts.append(budget.label)
    if (brand := chosen.get(BRAND)) and brand.choice != ANY:
        parts.append(brand.label)
    if (use := chosen_use(script, answers)) is not None:
        parts.append(word(use.words, lang))
    thing = word(script.words, lang)
    head = ", ".join(parts)
    if lang == TAMIL:
        sentence = (f"{head} - " if head else "") + f"ஒரு {thing}."
    else:
        sentence = f"A {thing.lower()}" + (f" - {head}" if head else "") + "."
    if added := extras(answers, lang):
        sentence += " " + ", ".join(added) + "."
    if (priority := chosen.get(PRIORITY)) and priority.choice != ANY:
        name = next(
            (word(p.words, lang) for p in script.priorities if p.id == priority.choice),
            "",
        )
        if name:
            sentence += (
                f" {name} முக்கியம்." if lang == TAMIL else f" {name} matters most."
            )
    if extra := chosen.get("consider"):
        remaining = [
            word(p.words, lang)
            for p in script.priorities
            if p.id in extra.choice.split(",")
            and (not chosen.get(PRIORITY) or p.id != chosen[PRIORITY].choice)
        ]
        if remaining:
            sentence += (
                (" Also important: " if lang == "en" else " அடுத்த முக்கியத்துவம் — ")
                + ", ".join(remaining)
                + "."
            )
    return sentence


def overview(
    script: Script,
    answers: Answers,
    lang: str = TAMIL,
    pending: tuple[tuple[str, str], ...] = (),
) -> str:
    """What was understood, said back before the next question.

    "சரிங்க, சைட்ல வீடியோ எடுக்க ₹25,000க்குள் Samsung போன் பாக்குறீங்க."
    Built only from answers that took - a brand the shelf could not offer
    in the budget was already dropped, so it is never claimed here.
    """
    chosen = {given.question: given for given in answers}
    # Said and understood but still waiting for its own question - a budget
    # said before the brand was asked - is said back too: "under ₹20,000"
    # heard and left out of the overview reads as not heard at all.
    waiting = {question: choice for question, choice in pending}
    brand = chosen.get(BRAND)
    brand_name = brand.label if brand and brand.choice != ANY else ""
    budget = chosen.get(BUDGET)
    budget_choice = budget.choice if budget else waiting.get(BUDGET, "")
    money = _budget_phrase(script, budget_choice, lang) if budget_choice else ""
    # The most specific use that has a phrase: "site video" says more than
    # "work", and a use with no phrase is left out rather than guessed.
    said_ids = {**waiting, **{given.question: given.choice for given in answers}}
    trail: list[Use] = []
    level_nodes = script.uses
    for question in (PURPOSE, *USE_LEVELS):
        node = next((u for u in level_nodes if u.id == said_ids.get(question)), None)
        if node is None:
            break
        trail.append(node)
        level_nodes = node.then
    used = next(
        (word(u.overview, lang) for u in reversed(trail) if word(u.overview, lang)), ""
    )
    thing = word(script.words, lang)
    if lang == TAMIL:
        said = " ".join(p for p in (used, money, f"{brand_name} {thing}".strip()) if p)
        return f"சரிங்க, {said} பாக்குறீங்க."
    noun = thing if thing.isupper() else thing.lower()
    named = f"{brand_name} {noun}".strip()
    article = "an" if named[:1] in "AEIOUaeiou" else "a"
    tail = "".join(f" {p}" for p in (money, used) if p)
    return f"Sure, you're looking for {article} {named}{tail}."


def _budget_phrase(script: Script, choice: str, lang: str) -> str:
    """A budget as it is said back: "₹25,000க்குள்" / "up to ₹25,000"."""
    if choice == ANY:
        return ""
    kind, _, figures = choice.partition(":")
    if kind == "cap" and figures.isdigit():
        high: int | None = int(figures)
        low: int | None = None
    elif kind == "floor" and figures.isdigit():
        low, high = int(figures), None
    elif kind == "range" and "-" in figures:
        first, _, second = figures.partition("-")
        low, high = int(first), int(second)
    else:
        band = next((b for b in bands(script, lang) if b.id == choice), None)
        if band is None:
            return ""
        low, high = band.low, band.high
    if low is None and high is not None:
        return f"{rupees(high)}க்குள்" if lang == TAMIL else f"up to {rupees(high)}"
    if high is None and low is not None:
        return f"{rupees(low)}க்கு மேல" if lang == TAMIL else f"above {rupees(low)}"
    if low is not None and high is not None:
        return (
            f"{rupees(low)} – {rupees(high)}-ல"
            if lang == TAMIL
            else f"between {rupees(low)} and {rupees(high)}"
        )
    return ""


def _depth(script: Script, answers: Answers) -> int:
    """How many levels of the use tree are already answered."""
    return sum(1 for given in answers if given.question in USE_LEVELS)


def answer(
    script: Script,
    items: tuple[Item, ...],
    answers: Answers,
    question: str,
    choice: str,
    lang: str = TAMIL,
) -> Answers:
    """These answers plus one more, if it is one of the buttons on screen."""
    step = next_step(script, items, answers, lang)
    if not isinstance(step, Ask) or step.question != question:
        raise NotOnOffer(f"{question!r} is not the question being asked")
    if question == BUDGET and (limit := exact_budget(choice, lang)):
        return (*answers, Answer(BUDGET, limit.id, limit.label))
    for offered in step.choices:
        if offered.id == choice:
            return (*answers, Answer(question, choice, offered.label))
    # A spoken brand arrives lowercased; the shelf spells it "Samsung".
    # Folding case here keeps the guarantee: still only buttons on offer.
    for offered in step.choices:
        if offered.id.lower() == choice.strip().lower():
            return (*answers, Answer(question, offered.id, offered.label))
    # Said or typed, a real value of this question that simply did not make
    # the eight buttons - "Apple", with Apple phones on the shelf - is taken:
    # it is still something the shelf holds, never something invented.
    if any(f.id == question for f in script.facets):
        held = {
            str(item.attributes.get(question))
            for item in matching(script, items, answers)
            if item.attributes.get(question)
        }
        for value in held:
            if value.lower() == choice.strip().lower():
                return (*answers, Answer(question, value, value))
    raise NotOnOffer(f"{choice!r} is not one of the answers to {question!r}")


def back(answers: Answers) -> Answers:
    """One answer fewer. Going back from the first leaves nothing, not an error."""
    return answers[:-1]
