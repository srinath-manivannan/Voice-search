"""What a page or a phone line can ask of a guided call.

    start       open a session, put the language question
    answer      one tap
    back        take the last tap back
    recommend   the matches, once there is nothing left to ask
    refine      the same matches, cheaper, or more like them - free
    pick        one product, asked of the estate again, live
    ask         one of the offered questions about the one they picked
    hear        what the customer SAID, pressing the buttons it means

Each returns the whole of what should be on screen next, as plain data. The
page draws it; a phone line will read `say` aloud and map `choices` onto
number keys. Neither is told how the step was arrived at, and neither can
send anything but the id of a button that was offered.

Two of them can cost money - `recommend` and `ask` - and both ask the
governor first. Everything else is a list already in memory being looked at
another way.
"""

from __future__ import annotations

import asyncio
import time
import logging
from typing import Any

from src.agent.tools import estate as estate_api
from src.guided import engine, spoken
from src.guided.stores import directions_url
from src.guided.answers import (
    CAPPED,
    DELIVERY,
    MOST_QUESTIONS,
    POLICY,
    UNSURE,
    Answer,
    Answerer,
    ProductFacts,
    facts_from,
)
from src.guided.answers import WORST_CASE_USD as QUESTION_WORST_CASE_USD
from src.guided.hear import (
    ASK,
    BACK,
    CHEAPER,
    COMMAND,
    LEAST_BYTES,
    LIST,
    MORE,
    MOST_BYTES,
    MOST_TURNS,
    NEXT,
    PICK,
    RESTART,
    Listener,
)
from src.guided.hear import WORST_CASE_USD as HEAR_WORST_CASE_USD
from src.guided.recommend import Pick, Recommendation, Recommender, ranked, reason
from src.guided.script import (
    rupees,
    CLASH_OTHER,
    FEATURE_WORDS,
    ANY,
    ANY_BUDGET,
    BRAND,
    BUDGET,
    BY_SHELF,
    INVITE_QUESTION,
    LANGUAGE,
    LANGUAGE_CHOICES,
    LANGUAGES,
    OVER_BUDGET,
    SCRIPTS,
    SEE_CHEAPER,
    PRIORITY,
    PURPOSE,
    SEE_MORE,
    SUGGEST_LIST,
    SUGGEST_NEXT,
    SHELF,
    SPEAK_BUTTON,
    TALK_TO_DEALER,
    SUGGESTED,
    TAMIL,
    USE,
    line,
    shelf_choices,
    word,
)
from src.guided.sessions import Governor, Session, Sessions
from src.guided.shelf import Item, ShelfBook
from src.guided.store import JourneyStore
from src.guided.thinker import WORST_CASE_USD
from src.guided.listen import figures
from src.guided.speech import SpeechBook
from src.guided.spoken import Sentence
from src.guided.voice import Recordings

log = logging.getLogger(__name__)

# How long a customer waits on the live check of the one they picked before
# being shown what the shelf already knew.
PICK_PATIENCE_SECONDS = 8.0
# Offers on a card. The first few are the ones a customer can use; the tail
# is the same bank's other tenures.
OFFERS_SHOWN = 3

QUESTION, READY, RESULT, NOTHING, CARD, ANSWER = (
    "question",
    "ready",
    "result",
    "nothing",
    "card",
    "answer",
)

# The one list a customer finally chooses from, as the owner asked: ten.
LIST_SHOW = 10

# What a card is built from, and what a question may be answered from
# besides: the spec sheet, the terms, delivery. Two requests, because the
# card must not wait on a spec sheet nobody may ask about.
# Cards whose facts are fetched ahead of a tap, and how long one counts as
# fetched: under the estate's own shortest cache (stock, 30 seconds).
# Questions the flow asks of its own accord, and answers that are not on the
# ladder. All of them go through `_special`, never past the checks.
CLASH, RELAX = "clash", "relax"
_SPECIAL = ("feature", "exclude", "prefer", CLASH, RELAX)
_SPECIAL_ANSWERS = ("feature", "exclude", "prefer")
PREFETCH_MOST = 10
PREFETCH_SECONDS = 25.0

_CARD_SECTIONS = (
    estate_api.PRICING,
    estate_api.STOCK,
    estate_api.OFFERS,
    estate_api.EMI,
)
_DETAIL_SECTIONS = (estate_api.PRODUCT, POLICY, DELIVERY)


class Flow:
    def __init__(
        self,
        book: ShelfBook,
        recommender: Recommender,
        sessions: Sessions,
        governor: Governor,
        estate: estate_api.Backend | None = None,
        recordings: Recordings | None = None,
        answerer: Answerer | None = None,
        speech: SpeechBook | None = None,
        listener: Listener | None = None,
        store: JourneyStore | None = None,
        map_prefix: str = "",
    ) -> None:
        self._book = book
        self._recommender = recommender
        self._sessions = sessions
        self.store = store or JourneyStore()
        self._governor = governor
        self._estate = estate
        self._recordings = recordings or Recordings()
        # With no model behind it every question is sent to the shop, which
        # is the right way for this to be switched off.
        self._answerer = answerer or Answerer()
        # The day's ceiling on everything paid (set by the app; None in tests
        # that do not care). The think checks it and adds to it.
        self.ledger: Any | None = None
        # Item code -> (when, the ask in flight or done) for card facts fetched
        # ahead of a tap. A tap joins it rather than asking again.
        self._prefetched: dict[
            str, tuple[float, asyncio.Future[dict[str, Any] | None]]
        ] = {}
        # None says nothing that was not recorded beforehand: the screen
        # still shows every sentence, and the call is simply quieter.
        self._speech = speech
        # None: the mic button is not offered, and taps are the only input.
        self._listener = listener
        self._map_prefix = map_prefix

    # ── the steps ─────────────────────────────────────────────────────────

    async def has_session(self, session_id: str) -> bool:
        return await self.store.exists(session_id, self._sessions)

    async def turn(
        self, method: str, sid: str, args: tuple[Any, ...], key: str, signature: str
    ) -> dict[str, Any]:
        return await self.store.run(
            self._sessions,
            sid,
            key,
            signature,
            lambda: getattr(self, method)(sid, *args),
        )

    def warm_enough(self) -> bool:
        """The phone shelf is verified and held: recommendations will not wait."""
        return bool(self._book.held("phone"))

    def warm(self) -> None:
        """Every shelf, one after another, the busiest first - so the first
        customer to tap a TV is not the one who waits for the TV shelf."""
        busiest = ("phone", "speaker")
        self._book.warm(
            (*busiest, *(s.shelf for s in SCRIPTS if s.shelf not in busiest))
        )

    async def start(self, *, verified: bool = False) -> dict[str, Any]:
        session = self._sessions.open()
        session.verified = verified
        await self.store.open(session)
        # The shelves start building while the first question is being
        # read, so that by the time a shelf is tapped it is usually there.
        self.warm()
        return await self._view(session)

    async def answer(
        self, session_id: str, question: str, choice: str
    ) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if await self._answer_one(session, question, choice):
            await self._apply_pending(session)
        self._unpick(session)
        session.shown, session.ranked, session.at = (), (), 0
        notice = await self._heal_empty(session)
        view = await self._view(session)
        if notice:
            view["say"] = notice
            view["audio"] = self._recordings.for_line(notice)
        return view

    async def _heal_empty(self, session: Session) -> str | None:
        """A brand and a budget that do not meet: say it, keep the budget.

        The owner's own script: "இந்த budget-ல இந்த brand available இல்ல,
        வேற brand choose பண்ணுங்க." The brand answer is taken back, the
        brand question returns - its buttons now counted inside the chosen
        budget - and the sentence above is said. The newest answer wins,
        because it is the one they just gave.
        """
        if session.shelf is None:
            return None
        script = BY_SHELF[session.shelf]
        items = await self._book.items(session.shelf)
        if not items or engine.matching(script, items, session.answers):
            return None
        for question in (facet.id for facet in script.facets):
            given = next(
                (
                    g
                    for g in session.answers
                    if g.question == question and g.choice != ANY
                ),
                None,
            )
            if given is None:
                continue
            without = tuple(g for g in session.answers if g is not given)
            if engine.matching(script, items, without):
                session.answers = without
                budget = next((g for g in without if g.question == BUDGET), None)
                if question == BRAND and budget is not None:
                    if offer := self._clash_offer(
                        session, script, items, given, budget.choice
                    ):
                        return offer
                return line("say_brand_not_in_budget", self._lang(session))
        return None

    async def _answer_one(self, session: Session, question: str, choice: str) -> bool:
        """One answer, if it is a button on offer right now. True if it took."""
        if question == LANGUAGE:
            if session.language is None and choice in LANGUAGES:
                session.language = choice
                return True
            return False
        if question in _SPECIAL:
            return await self._special(session, question, choice)
        if question == engine.CONFIRM and choice == "change":
            # "மாத்தணும்" with nothing named: back one question, the trail
            # above it stays tappable for anything further up.
            if session.answers:
                session.answers = engine.back(session.answers)
            return True
        if question == SHELF:
            if (
                session.language is not None
                and session.shelf is None
                and choice in BY_SHELF
            ):
                session.shelf = choice
                return True
            return False
        if session.shelf is None:
            return False
        items = await self._book.items(session.shelf)
        try:
            session.answers = engine.answer(
                BY_SHELF[session.shelf],
                items,
                session.answers,
                question,
                choice,
                self._lang(session),
            )
        except engine.NotOnOffer as refused:
            # A double tap, a stale page, or somebody trying their luck.
            # All three get the same thing: what is actually on screen.
            log.info("answer not on offer: %s", refused)
            return False
        return True

    @staticmethod
    def _implied_purpose(session: Session) -> tuple[str, str] | None:
        """ "Engineering" names the course; that it is for studies goes without
        saying. The ear, told "inginiyaring padikkiren", returned the course
        and not the purpose - so a remembered follow-up answer presses the one
        purpose it can only belong to."""
        if session.shelf is None:
            return None
        said = {choice for question, choice in session.pending if question == USE}
        for use in BY_SHELF[session.shelf].uses:
            if said & {child.id for child in use.then}:
                return (PURPOSE, use.id)
        return None

    async def _apply_pending(self, session: Session) -> list[str]:
        """Press what was said earlier, as each of its questions comes up."""
        applied: list[str] = []
        while session.pending:
            current = await self._current_question(session)
            match = next((p for p in session.pending if p[0] == current), None)
            if match is None and current == PURPOSE:
                match = self._implied_purpose(session)
            if match is None:
                break
            session.pending = tuple(p for p in session.pending if p != match)
            if await self._answer_one(session, *match):
                applied.append(match[1])
            else:
                # Named to its own question and still not a button: a brand
                # the shelf does not hold today. Remembered, so the reply
                # can be honest about it rather than a blank "say again".
                session.missed = (*session.missed, match[1])
        done = {given.question for given in session.answers}
        if session.language is not None:
            done.add(LANGUAGE)
        if session.shelf is not None:
            done.add(SHELF)
        session.pending = tuple(p for p in session.pending if p[0] not in done)
        return applied

    async def back(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        session.pending = ()
        if session.answers:
            session.answers = engine.back(session.answers)
        elif session.shelf is not None:
            session.shelf = None
        else:
            session.language = None
        self._unpick(session)
        session.shown, session.ranked, session.at = (), (), 0
        return await self._view(session)

    async def recommend(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        step = await self._ready(session)
        if step is None:
            return await self._view(session)

        if session.ranked:
            return self._result(
                session, Recommendation(session.shown, "rules"), "say_suggest"
            )

        def reserve() -> bool:
            # The owner's spec: the one bounded text call is for a VERIFIED
            # account, after the need is confirmed (`_ready` already insists
            # on the read-back), and only when rules cannot decide. A guest
            # gets the whole shopping flow, ranked by rule, at ₹0.
            if not session.verified or session.rank_attempted:
                return False
            if self.ledger is not None and not self.ledger.allows(
                self._governor.inr(WORST_CASE_USD)
            ):
                return False  # today's paid ceiling: rank by rule, free
            if not self._governor.allows(session, WORST_CASE_USD):
                return False
            directory = getattr(self, "directory", None)
            if directory is not None and not directory.reserve_rank(session.id).ok:
                return False
            session.rank_attempted = True
            session.reserved_usd = WORST_CASE_USD
            return True

        chosen = await self._recommender.choose(
            BY_SHELF[session.shelf or ""],
            session.answers,
            step,
            reserve,
            self._lang(session),
        )
        if chosen.usd and self.ledger is not None:
            self.ledger.add(self._governor.inr(chosen.usd))
        directory = getattr(self, "directory", None)
        if chosen.usd:
            session.reserved_usd = 0.0
            session.spent_usd += chosen.usd
            session.thinks += 1
            if directory is not None:
                directory.settle_rank(session.id)
        elif session.reserved_usd:
            session.reserved_usd = 0.0
            if directory is not None:
                directory.release_rank(session.id)
        # The ranking is worked out once; the customer is handed ONE, the
        # way a salesperson hands over one phone - "இது சரியா, வேற
        # பாக்கட்டுமா?" - and "show me another" walks forward from it.
        session.ranked = self._whole_order(session, step, chosen)
        session.rejected = ()
        session.at = 0
        session.shown = session.ranked[: engine.SHOW]
        return self._result(
            session,
            Recommendation(
                session.shown, chosen.source, chosen.usd, chosen.took_ms, chosen.tokens
            ),
            say_key="say_suggest",
        )

    def _whole_order(
        self, session: Session, step: engine.Ready, chosen: Recommendation
    ) -> tuple[Pick, ...]:
        """Everything on offer: the model's picks first, the rest by rule."""
        script = BY_SHELF[session.shelf or ""]
        lang = self._lang(session)
        priority = next((p for p in script.priorities if p.id == step.priority), None)
        over = {item.item_code for item in step.stretch}
        taken = {pick.item.item_code for pick in chosen.picks}
        rest = [
            item
            for item in ranked((*step.items, *step.stretch), priority)
            if item.item_code not in taken
        ]
        ordered = [
            *((pick.item, pick.why) for pick in chosen.picks),
            *((item, reason(item, priority, lang)) for item in rest),
        ]
        # "Vivo இருந்தா நல்லது": the preferred brand first, nothing else hidden
        # and the order within each group kept.
        preferred = {
            g.choice.lower() for g in session.answers if g.question == engine.PREFER
        }
        if preferred:
            ordered.sort(
                key=lambda pair: str(pair[0].brand or "").lower() not in preferred
            )
        return tuple(
            Pick(str(n), item, why, item.item_code in over)
            for n, (item, why) in enumerate(ordered, start=1)
        )

    async def refine(self, session_id: str, kind: str) -> dict[str, Any]:
        """The same matches, looked at the way the customer just asked to.

        `more` is the owner's "innum sollu": the NEXT four of the same
        ranking, added under the four already on screen, again and again
        until the shelf is out - and then the list stays and the call says
        "that is everything, choose from it". `cheaper` starts the list
        over, cheapest first. Both are free: nothing here is a model, it is
        the list in memory being re-cut.
        """
        session = self._sessions.get(session_id)
        if not session.ranked:
            return await self._view(session)

        if kind == NEXT:
            # "இதுல எதுவும் வேண்டாம், வேற காட்டு": the cards on screen are
            # turned down and never offered again in this ranking. With
            # nothing else left, everything is laid out and that is said.
            session.rejected = (
                *session.rejected,
                *(p.ref for p in session.shown if p.ref not in session.rejected),
            )
            left = self._not_rejected(session)
            session.at = 0
            if not left:
                session.shown = session.ranked[:LIST_SHOW]
                offers = await self._relax_offers(session)
                view = self._result(
                    session,
                    Recommendation(session.shown, "rules"),
                    "ask_relax" if offers else "say_no_more",
                )
                view["relax"] = offers
                return view
            session.shown = left[: engine.SHOW]
            return self._result(
                session, Recommendation(session.shown, "rules"), "say_suggest"
            )

        left = self._not_rejected(session) or session.ranked

        if kind == LIST:
            session.at = 0
            session.shown = self._capped(left, LIST_SHOW)
            return self._result(
                session, Recommendation(session.shown, "rules"), say_key="say_list"
            )

        if kind == MORE:
            if len(session.shown) >= len(left):
                return self._result(
                    session,
                    Recommendation(session.shown, "rules"),
                    say_key="say_no_more",
                )
            # Four more at a time, and never more than ten distinct cards.
            session.shown = self._capped(left, len(session.shown) + engine.SHOW)
            return self._result(
                session, Recommendation(session.shown, "rules"), say_key="say_more"
            )

        if kind == CHEAPER:
            by_price = tuple(
                sorted(left, key=lambda pick: (pick.item.price, pick.item.name))
            )
            session.at = 0
            session.shown = by_price[: engine.SHOW]
            return self._result(
                session, Recommendation(session.shown, "rules"), say_key="say_list"
            )
        return await self._view(session)

    @staticmethod
    def _not_rejected(session: Session) -> tuple[Pick, ...]:
        return tuple(p for p in session.ranked if p.ref not in session.rejected)

    @staticmethod
    def _capped(picks: tuple[Pick, ...], limit: int) -> tuple[Pick, ...]:
        """At most ``limit`` cards, one per model and storage."""
        seen: set[tuple[str, str]] = set()
        chosen: list[Pick] = []
        for pick in picks:
            item = pick.item
            attributes = getattr(item, "attributes", {}) or {}
            storage = str(attributes.get("storage") or attributes.get("ram") or "")
            key = (getattr(item, "name", "") or "", storage)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(pick)
            if len(chosen) >= limit:
                break
        return tuple(chosen)

    async def pick(self, session_id: str, ref: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        chosen = next((pick for pick in session.shown if pick.ref == ref), None)
        if chosen is None:
            return await self._view(session)
        lang = self._lang(session)
        self._unpick(session)
        session.picked = chosen.item.item_code
        payload = await self._card_facts(chosen.item.item_code)
        facts = _read(payload)
        if facts and facts.item_code and facts.item_code != chosen.item.item_code:
            facts, payload = None, None
        card = _card(chosen, facts, lang)
        budget = next(
            (
                engine.exact_budget(a.choice, lang)
                for a in session.answers
                if a.question == BUDGET
            ),
            None,
        )
        if card["stock"]["state"] == "out" or (
            budget and card["price_confirmed"] and not budget.holds(card["price"])
        ):
            self._unpick(session)
            session.ranked = tuple(p for p in session.ranked if p.ref != chosen.ref)
            session.at = 0
            session.shown = session.ranked[: engine.SHOW]
            message = (
                "The price or stock changed. Here are the remaining matches."
                if lang == "en"
                else "விலை அல்லது ஸ்டாக் மாறியிருக்கு. மீதமுள்ள தேர்வுகளைப் பாருங்க."
            )
            view = self._result(session, Recommendation(session.shown, "rules"))
            view.update(say=message, audio=None, speak={"text": message})
            return view
        session.latest_card = card
        session.branches = _branches(payload, prefix=self._map_prefix)
        # Started now and not awaited: the card is on screen while the spec
        # sheet is still arriving, and a question waits only for what is left.
        session.facts = asyncio.ensure_future(
            self._facts(chosen.item.item_code, card, payload)
        )
        return {
            **self._frame(session, CARD, line("say_card", lang)),
            "say": line("say_card", lang),
            "speak": self._spoken(session, spoken.card(card, lang)),
            "product": card,
            "questions": {
                "left": MOST_QUESTIONS,
                "invite": word(INVITE_QUESTION, lang),
                "choices": [
                    *(
                        {"id": key, "label": word(words, lang)}
                        for key, words in SUGGESTED
                    ),
                    {"id": "dealer", "label": word(TALK_TO_DEALER, lang)},
                ],
            },
        }

    async def ask(self, session_id: str, question: str) -> dict[str, Any]:
        """One of the question buttons under a card, by its id."""
        session = self._sessions.get(session_id)
        lang = self._lang(session)
        if session.picked and question in {
            "why",
            "emi",
            "where",
            "colours",
            "price",
            "offers",
        }:
            card = session.latest_card
            value = ""
            if question == "why":
                value = card.get("why", "")
            elif question == "price" and card.get("price_confirmed"):
                value = f"₹{card['price']:,}"
            elif question == "emi" and (plan := card.get("emi")):
                per = f"₹{plan['per_month']:,}"
                value = f"{per} per month" if lang == "en" else f"மாதம் {per}"
                if plan.get("months"):
                    value += (
                        f" for {plan['months']} months"
                        if lang == "en"
                        else f", {plan['months']} மாதங்கள்"
                    )
                if plan.get("provider"):
                    value += f" ({plan['provider']})"
            elif question == "where":
                value = ", ".join(b["name"] for b in session.branches if b.get("name"))
            elif question == "offers":
                value = ". ".join(
                    " ".join(str(x) for x in [o["label"], o.get("condition")] if x)
                    for o in card.get("offers", [])
                )
            elif question == "colours" and card.get("colours", 0) > 1:
                value = f"{card['colours']} " + (
                    "colours" if lang == "en" else "நிறங்கள்"
                )
            return self._answered(
                session, Answer(value or line("say_unsure", lang), bool(value), "rules")
            )
        if question == "dealer" and session.picked is not None:
            # Not a model question and not counted against the six: the
            # handover a sale ends with. Until telephony exists it names
            # the shops holding stock, where our person actually is.
            return {
                **self._frame(session, "dealer", line("say_dealer", lang)),
                "say": line("say_dealer", lang),
                "branches": list(session.branches),
            }
        asked = next(
            (word(words, lang) for key, words in SUGGESTED if key == question), None
        )
        if asked is None:
            # Not one of the buttons. Answered as "I am not sure" without
            # looking, and not counted: nothing was asked.
            return self._answered(
                session, Answer(line("say_unsure", lang), False, UNSURE)
            )
        # Whether anything is picked, and whether the six are spent, is
        # decided in one place for buttons and spoken questions alike.
        return await self._ask(session, asked)

    async def _ask(self, session: Session, asked: str) -> dict[str, Any]:
        """One question about the picked product, in words: a button's or theirs."""
        lang = self._lang(session)
        if session.picked is None:
            return await self._view(session)
        if session.facts is None:
            session.facts = asyncio.ensure_future(
                self._facts(session.picked, session.latest_card, None)
            )
        if session.asked >= MOST_QUESTIONS:
            return self._answered(
                session, Answer(line("say_enough", lang), False, CAPPED)
            )
        if not session.verified:
            from src.observability.metrics import METRICS

            METRICS.count(
                "guided_guest_blocked_total",
                "Paid guided calls refused for a guest",
            )
            return self._answered(
                session, Answer(line("say_unsure", lang), False, "guest")
            )
        given = await self._answerer.answer(
            session.picked,
            await session.facts,
            asked,
            lambda: self._governor.allows(session, QUESTION_WORST_CASE_USD),
            lang,
        )
        session.asked += 1
        if given.usd:
            session.spent_usd += given.usd
            session.thinks += 1
        return self._answered(session, given)

    async def heard_text(
        self, session_id: str, text: str, kind: str = "speech"
    ) -> dict[str, Any]:
        """The browser heard the customer for free; code presses the buttons.

        The owner's rule for the question phase - not one paisa - holds
        here by construction: `match.matches` is string work, the presses
        go through the same checks as taps, and the only paid thing it can
        reach is a question about an already-picked product, which goes to
        the governed answerer like any chip.
        """
        from src.guided import match

        session = self._sessions.get(session_id)
        if kind == "text":
            session.typed += 1
        else:
            session.said += 1
        lang = self._lang(session)
        # At the read-back every question is open to correction - the
        # newest word wins - so nothing is filtered as "already settled".
        current = await self._current_question(session)
        confirming = current == engine.CONFIRM
        settled = frozenset(g.question for g in session.answers) | frozenset(
            p[0] for p in session.pending
        )
        presses = tuple(
            match.matches(
                text,
                shelf=session.shelf,
                language_open=session.language is None,
                answered=frozenset() if confirming else settled,
                shown=len(session.shown),
                picked=session.picked is not None,
                confirming=confirming,
                current=current,
            )
        )
        shelf = session.shelf or next(
            (p.choice for p in presses if p.question == SHELF), ""
        )
        if shelf in BY_SHELF and not session.picked:
            wanted = match.named_priorities(BY_SHELF[shelf], text)
            if len(wanted) > 1 and not session.ranked:
                session.answers = tuple(
                    g
                    for g in session.answers
                    if g.question not in {"consider", PRIORITY, engine.CONFIRM}
                )
                session.answers += (
                    engine.Answer(
                        "consider",
                        ",".join(p.id for p in wanted),
                        ", ".join(word(p.words, lang) for p in wanted),
                    ),
                )
                presses = tuple(p for p in presses if p.question != PRIORITY)
            elif wanted and (PRIORITY not in settled or confirming):
                # The actual task is more specific than the occupational follow-up.
                if (
                    session.shelf
                    and any(a.question == PURPOSE for a in session.answers)
                    and not any(a.question == PRIORITY for a in session.answers)
                ):
                    session.answers += (
                        engine.Answer(
                            PRIORITY, wanted[0].id, word(wanted[0].words, lang)
                        ),
                    )
                    presses = tuple(p for p in presses if p.question != PRIORITY)
                    if not presses:
                        return {
                            **await self._view(session),
                            "heard": {
                                "text": text,
                                "pressed": [wanted[0].id],
                                "lang": lang,
                            },
                        }
        if presses and presses[0].question == "__yes__":
            first = await self._first_button(session)
            presses = (first,) if first else ()
        session.missed = ()
        view, pressed = await self._press(session, presses)
        clashed = await self._heal_empty(session) or await self._heal_spoken_budget(
            session, presses
        )
        if clashed:
            view = await self._view(session)
            view["say"] = clashed
            view["audio"] = self._recordings.for_line(clashed)
        asked_question = next((p.choice for p in presses if p.question == ASK), "")
        if (
            not asked_question
            and session.picked is not None
            and not pressed
            and len(text.strip()) > 3
        ):
            # On the card, words that are not a chip are their own question
            # - answered from the facts, governed, like typing a chip.
            view = await self._ask(session, text.strip()[:200])
            pressed.append("ask")
        if view is None:
            key = "say_not_that" if session.missed else "say_again"
            return self._unheard(session, key, text)
        # What was understood, said back before the next question - only when
        # this turn answered something about the product, never for "Tamil"
        # alone or a command like "show another".
        about_need = {SHELF, BRAND, BUDGET, PURPOSE, *engine.USE_LEVELS}
        # The language as it stands NOW: the same sentence may have chosen it
        # ("i need samsung phone under 20K" on the first screen is English),
        # and the overview is said in the language the call continues in.
        lang = self._lang(session)
        if (
            session.shelf is not None
            and pressed
            and any(p.question in about_need for p in presses)
            and view.get("step") in ("question", "ready")
        ):
            view["overview"] = {
                "text": engine.overview(
                    BY_SHELF[session.shelf], session.answers, lang, session.pending
                ),
                "display": engine.overview(
                    BY_SHELF[session.shelf], session.answers, "en", session.pending
                ),
                "speech": engine.overview(
                    BY_SHELF[session.shelf], session.answers, "ta", session.pending
                ),
                "audio": self._recordings.for_line(line("say_ack", lang)),
            }
        return {
            **view,
            "heard": {
                "text": text,
                "pressed": pressed,
                "remembered": [choice for _, choice in session.pending],
                "lang": lang,
            },
        }

    async def _heal_spoken_budget(
        self, session: Session, presses: tuple[Any, ...]
    ) -> str | None:
        """A spoken "Samsung, under 30 thousand" where Samsung has nothing
        under 30 thousand. The budget was said LAST of the two constraints
        to survive, per the owner's own script: say the clash, keep the
        budget, and ask the brand again with honest counts.
        """
        said_band = next(
            (p.choice for p in presses if p.question == BUDGET and p.choice != ANY),
            None,
        )
        if said_band is None or session.shelf is None:
            return None
        if any(given.question == BUDGET for given in session.answers):
            return None  # the budget landed; nothing clashed
        brand_given = next(
            (g for g in session.answers if g.question == BRAND and g.choice != ANY),
            None,
        )
        if brand_given is None:
            return None
        script = BY_SHELF[session.shelf]
        items = await self._book.items(session.shelf)
        without_brand = tuple(g for g in session.answers if g is not brand_given)
        band = next((b for b in engine.bands(script) if b.id == said_band), None)
        if band is None or not any(
            band.holds(item.price)
            for item in engine.matching(script, items, without_brand)
        ):
            return None  # the band is empty for everyone, not a brand clash
        # The budget is REMEMBERED, not forced: it presses its own button
        # the moment the brand is re-answered, through the same checks.
        session.answers = without_brand
        session.pending = (
            *(p for p in session.pending if p[0] != BUDGET),
            (BUDGET, said_band),
        )
        if offer := self._clash_offer(session, script, items, brand_given, said_band):
            return offer
        return line("say_brand_not_in_budget", self._lang(session))

    def _clash_offer(
        self,
        session: Session,
        script: Any,
        items: tuple[Any, ...],
        brand_given: engine.Answer,
        budget_choice: str,
    ) -> str | None:
        """The choice the owner's spec asks for: another brand at this budget,
        or this brand from its next price - the cheapest it holds above."""
        lang = self._lang(session)
        band = next(
            (b for b in engine.bands(script, lang) if b.id == budget_choice), None
        ) or engine.exact_budget(budget_choice, lang)
        if band is None or band.high is None:
            return None
        own = engine.matching(
            script,
            items,
            (*(g for g in session.answers if g.question != BUDGET), brand_given),
        )
        above = sorted(i.price for i in own if i.price >= band.high)
        if not above:
            return None
        next_band = next(
            (b for b in engine.bands(script, lang) if b.holds(above[0])), None
        )
        if next_band is None:
            return None
        session.clash = {
            "brand": brand_given.choice,
            "brand_label": brand_given.label,
            "band": next_band.id,
            "band_label": next_band.label,
            "from": above[0],
        }
        return line("ask_clash", lang)

    async def _special(self, session: Session, question: str, choice: str) -> bool:
        """Answers that are not ladder questions, applied where they belong.

        A feature is a must; an exclusion takes one brand off (and loosens a
        brand answer that named it); a preference ranks one brand first. The
        clash and relax choices change the answers they were offered for.
        """
        if session.shelf is None:
            return False
        script = BY_SHELF[session.shelf]
        lang = self._lang(session)
        items = await self._book.items(session.shelf)
        answers = session.answers
        facet = next((f for f in script.facets if f.id == BRAND), None)
        anyone = word(facet.any_words, lang) if facet else ""
        if question == engine.FEATURE:
            if choice not in FEATURE_WORDS or any(
                g.question == engine.FEATURE and g.choice == choice for g in answers
            ):
                return False
            name = word(FEATURE_WORDS[choice], lang)
            session.answers = (*answers, engine.Answer(engine.FEATURE, choice, name))
            return True
        if question in (engine.EXCLUDE, engine.PREFER):
            shown_name = next(
                (
                    str(i.attributes.get(BRAND))
                    for i in items
                    if str(i.attributes.get(BRAND, "")).lower() == choice.lower()
                ),
                choice.title(),
            )
            kept = tuple(
                g
                for g in answers
                if not (
                    g.question in (engine.EXCLUDE, engine.PREFER)
                    and g.choice.lower() == choice.lower()
                )
            )
            if question == engine.EXCLUDE:
                kept = tuple(
                    engine.Answer(BRAND, ANY, anyone)
                    if g.question == BRAND and g.choice.lower() == choice.lower()
                    else g
                    for g in kept
                )
            session.answers = (*kept, engine.Answer(question, choice, shown_name))
            return True
        if question == CLASH and session.clash:
            clash, session.clash = session.clash, None
            if choice != "same":
                return True  # another brand: the brand question, budget kept
            session.pending = tuple(p for p in session.pending if p[0] != BUDGET)
            session.answers = tuple(g for g in session.answers if g.question != BUDGET)
            took = await self._answer_one(session, BRAND, clash["brand"])
            if took:
                await self._answer_one(session, BUDGET, clash["band"])
            await self._apply_pending(session)
            return took
        if question == RELAX:
            verified = await self._book.items(session.shelf, verified=True)
            offers = engine.relaxations(script, verified, answers, lang)
            chosen = next(
                (o for o in offers if o.id == choice or o.id.startswith(f"{choice}:")),
                None,
            )
            if chosen is None:
                return False
            kind, _, value = chosen.id.partition(":")
            if kind == "budget":
                limit = engine.exact_budget(f"cap:{value}", lang)
                if limit is None:
                    return False
                session.answers = tuple(
                    engine.Answer(BUDGET, limit.id, limit.label)
                    if g.question == BUDGET
                    else g
                    for g in answers
                )
            elif kind == "brand":
                session.answers = tuple(
                    engine.Answer(BRAND, ANY, anyone) if g.question == BRAND else g
                    for g in answers
                )
            else:
                session.answers = tuple(
                    g for g in answers if not (g.question == kind and g.choice == value)
                )
            self._unpick(session)
            session.shown, session.ranked, session.at = (), (), 0
            return True
        return False

    async def _relax_offers(self, session: Session) -> list[dict[str, Any]]:
        """Which one change would bring more, for the "nothing more" screens."""
        if session.shelf is None:
            return []
        offers = engine.relaxations(
            BY_SHELF[session.shelf],
            await self._book.items(session.shelf, verified=True),
            session.answers,
            self._lang(session),
        )
        return [{"id": o.id, "label": o.label, "count": o.count} for o in offers[:4]]

    async def _first_button(self, session: Session) -> Any | None:
        """ "சரி" accepts whatever suggestion is on screen: its first button."""
        from src.guided.hear import Press

        view = await self._view(session)
        question = view.get("question")
        # Only where the first button IS an agreement: the suggested spec,
        # the read-back, "a little above is fine". On the brand question a
        # bare "ஓகே" pressed the first brand on the list (Nokia) - a yes is
        # not a brand.
        agreeing = {PRIORITY, engine.CONFIRM, engine.BUDGET_UP}
        if (
            isinstance(question, dict)
            and question["choices"]
            and question["id"] in agreeing
        ):
            return Press(str(question["id"]), str(question["choices"][0]["id"]))
        return None

    async def hear(self, session_id: str, pcm: bytes) -> dict[str, Any]:
        """What the customer said, pressed as the buttons it means.

        Returns the screen the presses led to, with `heard` beside it - or,
        when nothing was pressed, a `heard` step that leaves the screen as
        it is and says "please say it again".
        """
        session = self._sessions.get(session_id)
        lang = self._lang(session)
        if self._listener is None:
            return self._unheard(session, "say_tap", "")
        if getattr(self._listener, "paid", True) and not session.verified:
            from src.observability.metrics import METRICS

            METRICS.count(
                "guided_guest_blocked_total",
                "Paid guided calls refused for a guest",
            )
            return self._unheard(session, "say_tap", "")
        if not LEAST_BYTES <= len(pcm) <= MOST_BYTES:
            return self._unheard(session, "say_again", "")
        if session.heard >= MOST_TURNS or not self._governor.allows(
            session, HEAR_WORST_CASE_USD
        ):
            return self._unheard(session, "say_tap", "")

        heard = await self._listener.hear(await self._menu(session), pcm)
        session.heard += 1
        session.spent_usd += heard.usd

        session.missed = ()
        view, pressed = await self._press(session, heard.presses)
        if (
            heard.question
            and session.picked is not None
            and not any(p.question == ASK for p in heard.presses)
        ):
            view = await self._ask(session, heard.question)
            pressed.append(heard.question)
        if view is None:
            # "Nokia venum" when there is no Nokia: say that it is out of
            # stock and that what IS in stock is on screen - the screen
            # already lists it - rather than pretend not to have heard.
            key = "say_not_that" if session.missed else "say_again"
            return self._unheard(session, key, heard.text)
        return {
            **view,
            "heard": {
                "text": heard.text,
                "pressed": pressed,
                "remembered": [choice for _, choice in session.pending],
                "lang": lang,
            },
        }

    async def _press(
        self, session: Session, presses: tuple[Any, ...]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Apply presses as taps, each through the same checks a tap meets.

        Answers to the shelf's questions are applied when their question
        comes up, so one sentence can answer three of them in the order the
        engine asks, whatever order they were said in.
        """
        view: dict[str, Any] | None = None
        picked_view: dict[str, Any] | None = None
        pressed: list[str] = []
        specials = [p for p in presses if p.question in _SPECIAL]
        said = [
            (p.question, p.choice)
            for p in presses
            if p.question not in (PICK, ASK, COMMAND, *_SPECIAL)
        ]
        if said:
            # A correction at the read-back: the changed question's old
            # answer is dropped and the ladder replayed from the kept ones,
            # so it ends at the read-back again with the new summary.
            redone = {q for q, _ in said} & {g.question for g in session.answers}
            if redone:
                said = [
                    *said,
                    *(
                        (g.question, g.choice)
                        for g in session.answers
                        if g.question not in redone
                        and g.question not in (engine.CONFIRM, engine.BUDGET_UP)
                        and g.question not in _SPECIAL_ANSWERS
                    ),
                ]
                # Features, exclusions and preferences are not ladder answers:
                # they stay put while the ladder is replayed around them.
                session.answers = tuple(
                    g for g in session.answers if g.question in _SPECIAL_ANSWERS
                )
            # The newest word on a question wins: "Samsung - no, Vivo".
            asked = {question for question, _ in said}
            session.pending = (
                *(p for p in session.pending if p[0] not in asked),
                *said,
            )
            applied = await self._apply_pending(session)
            if applied:
                self._unpick(session)
                session.shown, session.ranked, session.at = (), (), 0
                view = await self._view(session)
                pressed += applied

        # After the ladder answers, so "போன் வேணும், Samsung வேண்டாம்" has a
        # shelf to take Samsung off.
        for special in specials:
            if await self._special(session, special.question, special.choice):
                self._unpick(session)
                session.shown, session.ranked, session.at = (), (), 0
                pressed.append(special.choice)
        if specials:
            view = await self._view(session)

        for press in presses:
            if press.question == COMMAND:
                if press.choice == BACK:
                    view = await self.back(session.id)
                elif press.choice == RESTART:
                    view = await self._restart(session)
                elif press.choice in (CHEAPER, MORE, NEXT, LIST) and session.shown:
                    view = await self.refine(session.id, press.choice)
                else:
                    continue
                pressed.append(press.choice)
            elif (
                press.question == PICK
                and press.choice.isdigit()
                and 1 <= int(press.choice) <= len(session.shown)
            ):
                view = await self.pick(
                    session.id, session.shown[int(press.choice) - 1].ref
                )
                picked_view = view
                pressed.append(press.choice)
            elif press.question == ASK and session.picked is not None:
                # "டீலர்கிட்ட பேசணும்" is the handover button, not one of the
                # suggested questions - it was heard and then dropped here.
                if press.choice == "dealer" or any(
                    key == press.choice for key, _ in SUGGESTED
                ):
                    view = await self.ask(session.id, press.choice)
                    pressed.append(press.choice)
        if picked_view is not None and view is not picked_view and view is not None:
            # "C option-க்கு EMI இருக்கா?" picks and asks in one breath: the
            # answer goes with the card it is about, or it lands under nothing.
            view = {
                **view,
                "product": picked_view.get("product"),
                "questions": picked_view.get("questions"),
            }
        return view, pressed

    async def _current_question(self, session: Session) -> str | None:
        view = await self._view(session)
        question = view.get("question")
        return str(question["id"]) if isinstance(question, dict) else None

    async def _restart(self, session: Session) -> dict[str, Any]:
        """From the first question again, in the same language."""
        session.shelf, session.answers, session.shown = None, (), ()
        session.pending = ()
        self._unpick(session)
        return await self._view(session)

    async def _menu(self, session: Session) -> dict[str, Any]:
        """What the customer can press right now, each item carrying its press.

        The first version listed cards and commands without saying how to
        press them, and "rendavathu phone kaatunga" - heard perfectly -
        pressed nothing. Now every item states the `question` and `id` that
        press it, and the ear only has to copy one.
        """
        lang = self._lang(session)

        def item(question: str, ident: str, label: str, **more: Any) -> dict[str, Any]:
            return {"question": question, "id": ident, "label": label, **more}

        commands = [
            item(COMMAND, BACK, "back / pinnaadi"),
            item(COMMAND, RESTART, "start again / muthalla irunthu"),
        ]
        if session.picked is not None:
            return {
                "on_screen": "the card of one product, and questions about it",
                "card_questions": [
                    item(ASK, key, word(words, lang)) for key, words in SUGGESTED
                ],
                "commands": commands,
            }
        if session.shown:
            return {
                "on_screen": "product cards, numbered in order",
                "cards": [
                    item(
                        PICK,
                        p.ref,
                        spoken.spoken_name(p.item.name),
                        price=p.item.price,
                        brand=p.item.brand,
                    )
                    for p in session.shown
                ],
                "commands": [
                    *commands,
                    item(COMMAND, NEXT, word(SUGGEST_NEXT, lang)),
                    item(COMMAND, LIST, word(SUGGEST_LIST, lang)),
                    item(COMMAND, CHEAPER, word(SEE_CHEAPER, lang)),
                    item(COMMAND, MORE, word(SEE_MORE, lang)),
                ],
            }
        menu: dict[str, Any] = {
            "on_screen": "a question and its buttons",
            "commands": commands,
        }
        if session.language is None:
            # "Enakku phone paakanum", said to the welcome screen, answers
            # two questions at once: the language it was said in, and the
            # shelf. Both are on the menu from the first word.
            menu["now"] = {
                "ask": line("ask_language"),
                "buttons": [
                    {"question": LANGUAGE, "id": c.id, "label": c.label}
                    for c in LANGUAGE_CHOICES
                ],
            }
            menu["also"] = [
                {
                    "question": SHELF,
                    "id": script.shelf,
                    "label": f"{script.words[0]} / {script.words[1]}",
                }
                for script in SCRIPTS
            ]
            return menu
        view = await self._view(session)
        question = view.get("question")
        if isinstance(question, dict):
            menu["now"] = {
                "ask": question["ask"],
                "buttons": [
                    item(str(question["id"]), c["id"], c["label"])
                    for c in question["choices"]
                ],
            }
        if session.shelf is not None:
            menu["also"] = self._also(session)
        return menu

    def _also(self, session: Session) -> list[dict[str, Any]]:
        """Every other answer this shelf can take, so one sentence can give several."""
        script = BY_SHELF[session.shelf or ""]
        lang = self._lang(session)
        held = self._book.held(script.shelf)
        also: list[dict[str, Any]] = [
            {"question": BUDGET, "id": b.id, "label": b.label}
            for b in engine.bands(script, lang)
        ]
        # "I don't mind" is an answer too. Without it, "budget pathi kavalai
        # illa" had nothing to press, and everything said after it was lost.
        also.append({"question": BUDGET, "id": ANY, "label": word(ANY_BUDGET, lang)})
        for facet in script.facets:
            values = sorted(
                {i.attributes[facet.id] for i in held if facet.id in i.attributes}
            )
            also += [{"question": facet.id, "id": v, "label": v} for v in values]
            also.append(
                {"question": facet.id, "id": ANY, "label": word(facet.any_words, lang)}
            )
        also += [
            {"question": PURPOSE, "id": u.id, "label": word(u.words, lang)}
            for u in script.uses
        ]
        also += [
            {"question": USE, "id": c.id, "label": word(c.words, lang)}
            for u in script.uses
            for c in u.then
        ]
        also += [
            {"question": PRIORITY, "id": p.id, "label": word(p.words, lang)}
            for p in script.priorities
        ]
        return also

    def _unheard(self, session: Session, key: str, text: str) -> dict[str, Any]:
        """Nothing pressed: the screen stays, and the customer is told why."""
        said = line(key, self._lang(session))
        return {
            **self._frame(session, "heard", said),
            "say": said,
            "heard": {"text": text, "pressed": [], "lang": self._lang(session)},
        }

    # ── the shared middles ────────────────────────────────────────────────

    @staticmethod
    def _lang(session: Session) -> str:
        return session.language or TAMIL

    async def _ready(self, session: Session) -> engine.Ready | None:
        """This session's finished answers, or None if it is mid-question."""
        if session.shelf is None:
            return None
        step = engine.next_step(
            BY_SHELF[session.shelf],
            # The one place that waits for a shelf the estate has checked:
            # a recommendation is made from verified items only.
            await self._book.items(session.shelf, verified=True),
            session.answers,
            self._lang(session),
        )
        if not any(
            g.question == engine.CONFIRM and g.choice == "yes" for g in session.answers
        ):
            return None
        if not isinstance(step, engine.Ready) or not (step.items or step.stretch):
            return None
        items = tuple(i for i in step.items if i.stock_confirmed and i.price_confirmed)
        stretch = tuple(
            i for i in step.stretch if i.stock_confirmed and i.price_confirmed
        )
        return engine.Ready(items, step.priority, stretch) if items or stretch else None

    def _unpick(self, session: Session) -> None:
        """Forget the pick, and stop fetching facts nobody will ask about."""
        if session.facts is not None and not session.facts.done():
            session.facts.cancel()
        session.latest_card = {}
        session.picked, session.facts, session.asked = None, None, 0

    def _answered(self, session: Session, given: Answer) -> dict[str, Any]:
        said = (
            self._spoken(session, Sentence(given.text, figures(given.text)))
            if given.known
            else None
        )
        return {
            **self._frame(session, ANSWER, given.text),
            "say": given.text,
            "speak": said,
            "known": given.known,
            "left": max(MOST_QUESTIONS - session.asked, 0),
            "how": {
                "source": given.source,
                "took_ms": given.took_ms,
                "tokens": given.tokens,
                "inr": round(self._governor.inr(given.usd), 3),
            },
        }

    async def _facts(
        self, item_code: str, card: dict[str, Any], payload: dict[str, Any] | None
    ) -> ProductFacts:
        """The page a question is answered from. Never fails: at worst it is
        the card alone, which is still true and still answers "how much?"."""
        details = await self._estate_says(item_code, _DETAIL_SECTIONS)
        return facts_from(card, payload, details)

    # ── what goes back ────────────────────────────────────────────────────

    def _frame(self, session: Session, step: str, said: str) -> dict[str, Any]:
        """What every answer carries, whatever step it is.

        `said` is the sentence this step says. `audio` names its recording,
        or is None when there is no recording that still says exactly that -
        in which case the sentence is shown and nothing is played, which is
        the only safe thing to do with a recording of some other sentence.
        """
        lang = self._lang(session)
        answered = []
        if session.shelf is not None:
            answered.append(
                {"question": SHELF, "label": word(BY_SHELF[session.shelf].words, lang)}
            )
        answered.extend(
            {"question": given.question, "label": given.label}
            for given in session.answers
        )
        return {
            "session": session.id,
            "lang": lang,
            "step": step,
            "answered": answered,
            "audio": self._recordings.for_line(said),
            # What to say, and play, if the next step keeps them waiting.
            "waiting": {
                "say": line("say_thinking", lang),
                "audio": self._recordings.for_line(line("say_thinking", lang)),
            },
            "meter": self._governor.meter(session),
            # For the developer drawer: the shelf's four counts, and how many
            # of its models the answers so far still match.
            "shelf_counts": self._book.counts.get(session.shelf or ""),
            # The shopping brief - must, prefer, priority, unknown - for the
            # app; the read-back says the same thing in words.
            "brief": (
                engine.brief(BY_SHELF[session.shelf], session.answers, lang)
                if session.shelf is not None
                else None
            ),
            "mic": (
                {"label": word(SPEAK_BUTTON, lang)}
                if self._listener is not None and session.heard < MOST_TURNS
                else None
            ),
        }

    async def _view(self, session: Session) -> dict[str, Any]:
        """Where this session stands, worked out from its answers alone."""
        lang = self._lang(session)
        if session.language is None:
            return {
                **self._frame(session, QUESTION, line("ask_language")),
                "question": {
                    "id": LANGUAGE,
                    "ask": line("ask_language"),
                    "choices": [
                        {"id": c.id, "label": c.label} for c in LANGUAGE_CHOICES
                    ],
                },
            }
        if session.shelf is None:
            return {
                **self._frame(session, QUESTION, line("ask_shelf", lang)),
                "question": {
                    "id": SHELF,
                    "ask": line("ask_shelf", lang),
                    "choices": [
                        {"id": c.id, "label": c.label, "count": known}
                        for c in shelf_choices(lang)
                        # Known to be empty is not offered. Not yet counted
                        # is: the alternative is a first screen that waits
                        # on every shelf in the shop.
                        if (known := self._book.known(c.id)) != 0
                    ],
                },
            }
        if session.clash and not any(g.question == BRAND for g in session.answers):
            clash = session.clash
            return {
                **self._frame(session, QUESTION, line("ask_clash", lang)),
                "question": {
                    "id": CLASH,
                    "ask": line("ask_clash", lang),
                    "note": (
                        f"{clash['brand_label']}: {rupees(clash['from'])} முதல் இருக்கு"
                        if lang == TAMIL
                        else f"{clash['brand_label']}: from {rupees(clash['from'])}"
                    ),
                    "choices": [
                        {"id": "other", "label": word(CLASH_OTHER, lang)},
                        {
                            "id": "same",
                            "label": f"{clash['brand_label']}, {clash['band_label']}",
                        },
                    ],
                },
            }
        step = engine.next_step(
            BY_SHELF[session.shelf],
            await self._book.items(session.shelf),
            session.answers,
            lang,
        )
        if isinstance(step, engine.Ask):
            return {
                **self._frame(session, QUESTION, step.ask),
                "question": {
                    "id": step.question,
                    "ask": step.ask,
                    "note": step.note,
                    "choices": [
                        {"id": c.id, "label": c.label, "count": c.count}
                        for c in step.choices
                    ],
                },
            }
        if not any(
            i.stock_confirmed and i.price_confirmed
            for i in (*step.items, *step.stretch)
        ):
            return {
                **self._frame(session, NOTHING, line("say_nothing", lang)),
                "say": line("say_nothing", lang),
                "relax": await self._relax_offers(session),
            }
        return {
            **self._frame(session, READY, line("say_thinking", lang)),
            "say": line("say_thinking", lang),
            "left": len(step.items) or len(step.stretch),
        }

    def _result(
        self, session: Session, chosen: Recommendation, say_key: str = ""
    ) -> dict[str, Any]:
        # The cards on screen are the ones a customer taps next. Their live
        # facts start now, so the tap finds them cached in the estate rather
        # than waiting the 2-3 seconds a cold ask takes (measured 2026-11-24).
        self._prefetch(tuple(pick.item.item_code for pick in chosen.picks))
        lang = self._lang(session)
        if not say_key:
            # "say_result" is retired: three recordings of it were heard
            # as "touch YOU" or "start", and the suggestion line says the
            # same thing over four cards.
            say_key = "say_one" if len(chosen.picks) == 1 else "say_suggest"
        say = line(say_key, lang)
        picks = [_summary(pick, lang) for pick in chosen.picks]
        # Only what is new is read aloud: one suggestion, or the last ten.
        fresh = (
            picks[-engine.SHOW : -engine.SHOW + 1]
            if say_key == "say_more" and len(picks) >= engine.SHOW
            else picks[:1]
        )
        return {
            **self._frame(session, RESULT, say),
            "say": say,
            "speak": (
                self._spoken(session, spoken.results(fresh, lang))
                if say_key != "say_no_more"
                else None
            ),
            "picks": picks,
            "suggesting": say_key == "say_suggest",
            "decision": {
                "rules_version": 1,
                "shelf": session.shelf,
                "language": lang,
                "confirmed": any(
                    a.question == engine.CONFIRM and a.choice == "yes"
                    for a in session.answers
                ),
                "answers": [(a.question, a.choice) for a in session.answers],
                "source": chosen.source,
                "matches": [
                    {
                        "sku": pick.item.item_code,
                        "price": pick.item.price,
                        "why": pick.why,
                        "evidence": {
                            "specs": "search-proxy",
                            "pim_fields": list(pick.item.pim_fields),
                            "price": "pricing"
                            if pick.item.price_confirmed
                            else "search-proxy",
                            "stock": "central-inventory"
                            if pick.item.stock_confirmed
                            else "unknown",
                            "checked_at": pick.item.checked_at,
                        },
                    }
                    for pick in chosen.picks
                ],
            },
            "more": (
                [
                    {"id": NEXT, "label": word(SUGGEST_NEXT, lang)},
                    {"id": LIST, "label": word(SUGGEST_LIST, lang)},
                ]
                if say_key == "say_suggest"
                else [
                    {"id": MORE, "label": word(SEE_MORE, lang)},
                    {"id": CHEAPER, "label": word(SEE_CHEAPER, lang)},
                ]
            ),
            # For the developer drawer. How the five were arrived at is the
            # first question about any bill, and it names no model.
            "how": {
                "source": chosen.source,
                "took_ms": chosen.took_ms,
                "tokens": chosen.tokens,
                "inr": round(self._governor.inr(chosen.usd), 3),
            },
        }

    def _spoken(
        self, session: Session, sentence: Sentence | None
    ) -> dict[str, Any] | None:
        """Ask for a sentence to be spoken; the page plays it when it lands.

        Admitted by the governor against the most a sentence can cost, and
        charged to this session when it is made - or not at all, when an
        earlier customer already paid for exactly these words.
        """
        if sentence is None:
            return None
        if self._speech is None:
            return {"text": sentence.text}

        def charge(usd: float) -> None:
            session.spent_usd += usd

        key = self._speech.request(
            sentence.text,
            self._lang(session),
            lambda most: self._governor.allows(session, most),
            charge,
            sentence.must_hear,
        )
        return {"key": key, "text": sentence.text} if key else None

    async def speech(self, key: str) -> bytes | None:
        """A spoken sentence, once it is made and checked. None: do not play."""
        if self._speech is None:
            return None
        return await self._speech.fetch(key)

    def _prefetch(self, codes: tuple[str, ...]) -> None:
        """Ask the estate about these now, without waiting and without fuss.

        the retailer's own services, so nothing is billed; the estate keeps each
        answer for a while, which is what makes the later tap instant. Each
        code is asked at most once in PREFETCH_SECONDS, so walking back and
        forth through the list does not ask again.
        """
        if self._estate is None:
            return
        now = time.monotonic()
        self._prefetched = {
            code: held
            for code, held in self._prefetched.items()
            if now - held[0] < PREFETCH_SECONDS
        }
        for code in codes[:PREFETCH_MOST]:
            if code in self._prefetched:
                continue
            self._prefetched[code] = (
                now,
                asyncio.ensure_future(self._estate_says(code, _CARD_SECTIONS)),
            )

    async def _card_facts(self, code: str) -> dict[str, Any] | None:
        """The card's live facts: the ask already made ahead, if it is fresh,
        or a new one. Joining it is the whole point of making it early."""
        held = self._prefetched.get(code)
        if held is not None and time.monotonic() - held[0] < PREFETCH_SECONDS:
            ask = held[1]
            if not ask.cancelled():
                try:
                    if (early := await asyncio.shield(ask)) is not None:
                        return early
                except Exception as failed:  # noqa: BLE001 - fall back to asking again
                    log.info("the early card ask failed for %s: %s", code, failed)
        # The early ask came back empty - timed out on a busy estate - or
        # there was none: the tap asks for itself. Joining an empty answer
        # once showed cards with no live price, stock or EMI (2026-11-24).
        return await self._estate_says(code, _CARD_SECTIONS)

    async def _estate_says(
        self, item_code: str, sections: tuple[str, ...]
    ) -> dict[str, Any] | None:
        """One product, asked live. None means we could not ask."""
        if self._estate is None:
            return None
        try:
            return await asyncio.wait_for(
                self._estate.product(item_code, sections), PICK_PATIENCE_SECONDS
            )
        except (estate_api.EstateUnavailable, asyncio.TimeoutError) as down:
            log.info("could not ask the estate about %s: %s", item_code, down)
            return None


def _branches(
    payload: dict[str, Any] | None, *, prefix: str = ""
) -> tuple[dict[str, Any], ...]:
    """The shops holding stock, best-stocked first, enough to send someone to."""
    sections = (payload or {}).get("sections")
    stock = sections.get("stock") if isinstance(sections, dict) else None
    data = (
        stock.get("data")
        if isinstance(stock, dict) and stock.get("status") == "ok"
        else None
    )
    held = (data or {}).get("branches")
    rows: list[tuple[int, dict[str, Any]]] = []
    for b in held if isinstance(held, list) else []:
        if not (isinstance(b, dict) and (b.get("label") or b.get("name"))):
            continue
        quantity = int(b["quantity"]) if str(b.get("quantity", "")).isdigit() else 0
        shop = {
            "name": str(b.get("label") or b.get("name") or "").strip(),
            "state": str(b.get("state") or "").strip(),
            "quantity": quantity or None,
            # Where it is, for directions: the estate gives no phone
            # number, so the handover is the way there.
            "area": str(b.get("area") or "").strip(),
            "city": str(b.get("city") or "").strip(),
            "address": str(b.get("address") or "").strip(),
            "pincode": str(b.get("pincode") or b.get("pin") or "").strip(),
        }
        geo = b.get("geo") if isinstance(b.get("geo"), dict) else None
        if geo:
            shop["geo"] = geo
        for key in (
            "lat",
            "lng",
            "latitude",
            "longitude",
            "maps_url",
            "map_url",
        ):
            if b.get(key) not in (None, ""):
                shop[key] = b.get(key)
        shop["directions"] = directions_url(shop, prefix=prefix)
        rows.append((quantity, shop))
    rows.sort(key=lambda pair: -pair[0])
    return tuple(row for _, row in rows[:8])


def _read(payload: dict[str, Any] | None) -> estate_api.Facts | None:
    if payload is None:
        return None
    try:
        return estate_api.read(payload)
    except estate_api.EstateUnavailable:
        return None


def _summary(pick: Pick, lang: str = TAMIL) -> dict[str, Any]:
    item = pick.item
    return {
        # No item codes and no sourcing here: this dict goes to the page,
        # and the page never names the model or a code. The audit trail
        # with both lives in the decision record, which the store keeps
        # and strips before the payload leaves.
        "ref": pick.ref,
        "name": item.name,
        "brand": item.brand,
        "price": item.price,
        "was": item.was,
        "why": pick.why,
        "specs": [list(spec) for spec in item.specs],
        "branches": item.branches,
        "colours": item.colours,
        "stock_confirmed": item.stock_confirmed,
        "price_confirmed": item.price_confirmed,
        # Said, not slipped in: the page prints this line on the card.
        "over_budget": word(OVER_BUDGET, lang) if pick.over else None,
    }


def _stock(item: Item, facts: estate_api.Facts | None) -> dict[str, Any]:
    """In, out, or honestly unknown. Never "out" because nobody answered."""
    if facts is not None and facts.stock.state == estate_api.KNOWN:
        return {
            "state": "in" if facts.stock.in_stock else "out",
            "quantity": facts.stock.quantity,
            "branches": facts.stock.branches,
        }
    return {"state": "unknown", "quantity": None, "branches": None}


def _card(
    pick: Pick, facts: estate_api.Facts | None, lang: str = TAMIL
) -> dict[str, Any]:
    item = pick.item
    price, was, confirmed = item.price, item.was, False
    if (
        facts is not None
        and facts.price.state == estate_api.KNOWN
        and facts.price.selling
    ):
        price, confirmed = facts.price.selling, True
        listed = facts.price.list_price
        was = listed if listed and listed > price else None

    offers: list[dict[str, Any]] = []
    effective = None
    if facts is not None and facts.offers.state == estate_api.KNOWN:
        effective = facts.offers.effective_price
        offers = [
            # The condition travels with the amount. "2,000 off" is not true;
            # "2,000 off on an HDFC card" is.
            {"label": o.label, "amount": o.amount, "condition": o.condition}
            for o in facts.offers.items[:OFFERS_SHOWN]
        ]

    emi = None
    if (
        facts is not None
        and facts.emi.state == estate_api.KNOWN
        and facts.emi.per_month
    ):
        emi = {
            "per_month": facts.emi.per_month,
            "months": facts.emi.months,
            "provider": facts.emi.provider,
            "kind": facts.emi.kind,
        }

    return {
        **_summary(pick, lang),
        "price": price,
        "was": was,
        "price_confirmed": confirmed,
        "stock": _stock(item, facts),
        "offers": offers,
        "offers_confirmed": bool(facts and facts.offers.state == estate_api.KNOWN),
        "price_with_best_offer": effective if effective and effective < price else None,
        "emi": emi,
        # False when the estate could not be asked just now, so the page
        # can say the details are as of a few minutes ago.
        "checked_live": bool(
            confirmed and facts and facts.stock.state == estate_api.KNOWN
        ),
    }
