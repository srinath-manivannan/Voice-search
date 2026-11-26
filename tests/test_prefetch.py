"""Reading a search out of a half-finished sentence.

The phrasings here are the ones the project has actually seen: the worked
example in persona.py, the handover note measured on a rotated socket, and the
arguments the model passed on the traced call of 2026-11-13.

Most of these assert that NOTHING is returned. That is the point of the
module: it is called on every fragment of partial transcription and the right
answer is almost always "not yet".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agent.prefetch import (
    Intent,
    Prefetcher,
    read_budget,
    read_category,
    read_intent,
)


class TestWhatFires:
    def test_the_phrasing_from_the_worked_example(self):
        assert read_intent("20-30k kulla camera phone venum") == Intent(
            category="phone", min_price=20000, max_price=30000
        )

    def test_the_phrasing_from_the_measured_handover(self):
        # "camera phone between 20,000 and 25,000 rupees", as the caller said
        # it on the call that was actually traced.
        assert read_intent("20,000-la irundhu 25,000 rupees kulla camera phone") == (
            Intent(category="phone", min_price=20000, max_price=25000)
        )

    def test_a_ceiling_with_no_floor(self):
        assert read_intent("25k kulla oru phone") == Intent(
            category="phone", min_price=None, max_price=25000
        )

    def test_tamil_script(self):
        assert read_intent("25000 ரூபாய் குள்ள ஒரு மொபைல்") == Intent(
            category="phone", min_price=None, max_price=25000
        )

    def test_a_shelf_that_is_not_phones(self):
        assert read_intent("50k kulla laptop venum") == Intent(
            category="laptop", min_price=None, max_price=50000
        )

    def test_the_args_match_what_the_model_would_have_sent(self):
        # The traced call's own arguments, so the prefetch lands in the cache
        # under the key the model's call produces.
        intent = read_intent("20000 to 25000 phone")
        assert intent is not None
        assert intent.as_args() == {
            "category": "phone",
            "max_price": 25000,
            "min_price": 20000,
        }


class TestWhatDoesNotFire:
    def test_a_category_with_no_budget(self):
        assert read_intent("phone irukka?") is None

    def test_a_budget_with_no_category(self):
        assert read_intent("25k kulla edhuvum irukka?") is None

    def test_two_shelves_is_ambiguous_not_narrow(self):
        assert read_intent("30k kulla phone illa laptop") is None

    def test_a_bare_quantity_is_not_a_price(self):
        # "show me 5 phones" is not a five thousand rupee budget, and more to
        # the point there is no ceiling word here at all.
        assert read_intent("5 phone kaattunga") is None

    def test_a_floor_without_a_ceiling_IS_searchable(self):
        """This asserted the opposite, and the opposite was wrong.

        `search_products` takes min_price on its own, and the model calls it
        that way - on the traced call of 2026-11-16 it asked for a laptop
        above 40,000, with no ceiling, and got three. The prefetch read the
        same sentence as no budget and sat the turn out, which cost a silent
        generation nobody heard.
        """
        intent = read_intent("20k mela phone venum")
        assert intent is not None
        assert (intent.min_price, intent.max_price) == (20000, None)

    def test_a_number_too_large_to_have_been_heard_right(self):
        assert read_budget("9000000 kulla") is None

    def test_an_empty_fragment(self):
        assert read_intent("") is None
        assert read_category("") is None
        assert read_budget("") is None

    def test_a_backwards_range(self):
        assert read_budget("30k - 20k") is None


class TestTheUnitRule:
    def test_k_is_thousands(self):
        assert read_budget("25k kulla") == (None, 25000)

    def test_a_small_bare_number_in_a_range_is_thousands(self):
        # "20-25 kulla" on a call about phones is not twenty rupees.
        assert read_budget("20-25 kulla") == (20000, 25000)

    def test_a_full_number_is_taken_as_written(self):
        assert read_budget("24,999 kulla") == (None, 24999)

    def test_the_unit_carries_across_a_range(self):
        assert read_budget("20 to 25k") == (20000, 25000)


# --- running it ------------------------------------------------------------


class FakeTools:
    """A ToolSession as far as the prefetcher can tell."""

    def __init__(self, result: dict[str, Any] | Exception, shortlist: tuple = ()):
        self._result = result
        self._shortlist = shortlist
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def shortlist(self) -> tuple:
        return self._shortlist

    async def search_ahead(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("search_ahead", args))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    emi: dict[str, Any] | None = None

    async def emi_ahead(self) -> dict[str, Any] | None:
        return self.emi


FOUND = {
    "total_matching": 4,
    "products": [
        {
            "ref": "1",
            "name": "Poco M8 5G",
            "price_inr": 19999,
            "specs": {"camera": "45MP"},
        },
        {
            "ref": "2",
            "name": "Realme P4 Lite 5G",
            "price_inr": 20999,
            "specs": {"battery": "5500mAh"},
        },
    ],
}


def primer() -> tuple[list[str], Any]:
    said: list[str] = []

    async def prime(text: str) -> None:
        said.append(text)

    return said, prime


async def test_it_searches_before_the_model_asks_and_primes_the_result():
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("naan 20-25k kulla oru camera phone")
    assert pre.task is not None
    await pre.task

    # The arguments are the ones the model's own call would have carried, so
    # a call that arrives anyway is answered from the session's cache.
    assert tools.calls == [
        (
            "search_ahead",
            {"category": "phone", "max_price": 25000, "min_price": 20000},
        )
    ]
    assert pre.fired and pre.used
    assert "Poco M8 5G, 19,999 rupees" in said[0]
    # Without this line the model reads the shortlist and searches anyway.
    assert "Do not call search_products" in said[0]


async def test_a_fragment_that_says_only_half_starts_nothing():
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    for fragment in ("naan", "naan oru phone", "naan oru phone venum"):
        pre.heard(fragment)

    assert pre.task is None
    assert tools.calls == []


async def test_it_does_not_rebuild_a_shortlist_for_the_same_request():
    """Moving the refs has to buy something.

    Once "the second one" means something to the customer, a search repoints
    those refs at other products. Doing that for a request already answered
    buys nothing at all, so the intent has to have CHANGED.
    """
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone venum")
    first = pre.task
    assert first is not None
    await first
    pre.heard("enakku 20-25k kulla phone venum, sollunga")

    assert pre.task is first
    assert len(tools.calls) == 1


async def test_a_customer_who_changes_their_mind_is_prefetched_again():
    """The measured case, and the reason the once-a-call cap went.

    On the call of 2026-11-16 the customer moved from 30-40k to 40k-up to
    20-30k. Three searches, six of the call's seven generations, and the
    prefetch fired for none of them - it had already spent its one turn.

    The refs move when the budget changes whether or not this fires, because
    the model searches again itself and `_search` only reuses a shortlist for
    a search with the same key. The cap only made them move a generation
    later, and charged for the generation.
    """
    tools = FakeTools(FOUND, shortlist=("already", "searched"))
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("30-40k kulla phone venum")
    assert pre.task is not None
    await pre.task
    pre.heard("illa, 20-30k kulla paarunga")
    assert pre.task is not None
    await pre.task

    assert len(tools.calls) == 2


async def test_it_stops_after_a_few_however_much_the_caller_wanders():
    """A transcript that keeps re-reading as a new intent must not sit in a
    loop asking the catalogue."""
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    for ceiling in (20, 25, 30, 35, 40, 45):
        pre.heard(f"{ceiling}k kulla phone venum")
        if pre.task is not None:
            await pre.task

    assert len(tools.calls) == Prefetcher.MAX_PER_CALL


async def test_it_fires_once_however_many_fragments_match():
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    first = pre.task
    pre.heard("20-25k kulla phone venum enakku")
    assert pre.task is first
    assert first is not None
    await first
    assert len(tools.calls) == 1


async def test_a_search_that_blows_up_leaves_the_call_alone():
    tools = FakeTools(RuntimeError("upstream is down"))
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task

    assert said == []
    assert not pre.fired and not pre.used


async def test_a_search_that_finds_nothing_primes_nothing():
    tools = FakeTools({"total_matching": 0, "products": []})
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task

    assert pre.fired and not pre.used
    assert said == []


async def test_a_catalogue_error_is_not_primed_as_a_shortlist():
    tools = FakeTools({"error": "catalogue_unavailable"})
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task

    assert said == []
    assert not pre.used


async def test_the_note_carries_the_specs_or_the_model_invents_a_reason():
    """Found by probing the live endpoint, not by reading the code.

    The first version of `note` carried names and prices only. Primed with
    it, the real model answered "Realme P4 Lite 5G, 20,999 rupees - great
    camera". Nothing had said anything about a camera.

    persona.py requires one reason per product "from the specs the tool
    returned", and the search result carries that field with a comment saying
    why: without it, asked why it suggested something, it made a camera up.
    Dropping it from the note re-opened exactly that bug.
    """
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task

    assert "45MP" in said[0]
    assert "5500mAh" in said[0]


def test_the_note_carries_stock_as_the_search_said_it():
    """On the cascade this note is all the brain sees of the search. Without
    stock, "stock irukka?" got persona.py's own example back word for word:
    "Ippo confirm panna mudiyathu" (2026-11-26)."""
    from src.agent.prefetch import note

    found = {
        "products": [
            {
                "ref": "1",
                "name": "Poco M8 5G",
                "stock": {"state": "known", "available": True},
            },
            {
                "ref": "2",
                "name": "Realme P4",
                "stock": {"state": "known", "available": False},
            },
            {"ref": "3", "name": "Galaxy A07", "in_stock_hint": True},
            {"ref": "4", "name": "Moto G06", "stock": {"state": "unknown"}},
        ]
    }
    said = note(found, Intent(category="phone", min_price=None, max_price=25000))
    assert "1) Poco M8 5G (in stock)" in said
    assert "2) Realme P4 (out of stock)" in said
    assert "3) Galaxy A07 (listed in stock)" in said
    # Could not ask and no hint: nothing is claimed either way.
    assert "4) Moto G06\n" in said


class TestTheBudgetSaidInWords:
    """A budget spoken as Tamil words, which is how a caller says it.

    read_budget runs digitise first, so everything below arrives at the same
    patterns as "25000". These are here because the prefetch is the whole
    saving: when it fires the model is handed a shortlist and answers in one
    generation, and when it does not the call pays for two.
    """

    def test_a_range_in_words(self):
        assert read_intent(
            "எனக்கு இருபதாயிரம் முதல் இருபத்தைந்தாயிரம் வரை ஒரு போன் வேணும்"
        ) == Intent(category="phone", min_price=20000, max_price=25000)

    def test_a_ceiling_in_words(self):
        assert read_intent("இருபத்தைந்தாயிரம் kulla oru phone") == Intent(
            category="phone", min_price=None, max_price=25000
        )

    def test_the_dative_between_the_number_and_the_ceiling(self):
        """ "25000-க்கு உள்ள" - and the reason it did not work.

        The dative was matched with a trailing `\\b`, and க்கு ends in the
        vowel sign ு, which is a combining mark and not a word character. A
        boundary between two non-word characters does not exist, so the
        branch could never match. Latin `ku` still needs its `\\b` or it eats
        the front of "kulla".
        """
        assert read_intent("இருபத்தைந்தாயிரம்-க்கு உள்ள போன்") == Intent(
            category="phone", min_price=None, max_price=25000
        )
        assert read_intent("25k-ku keezha phone") == Intent(
            category="phone", min_price=None, max_price=25000
        )
        assert read_intent("25000 kulla phone") == Intent(
            category="phone", min_price=None, max_price=25000
        )

    def test_ulla_on_its_own_is_not_a_ceiling(self):
        """உள்ள is also the ordinary word for "in".

        It is in the ceiling list, so this is the test that says it is only
        ever read when a number sits immediately in front of it.
        """
        assert read_intent("stock-ல உள்ள phone kaamunga") is None


class TestTheWiderShelves:
    """Poorvika is not a phone shop, and for a while this code thought it was.

    The tool's category was an enum of five, so a customer asking for an AC
    or a washing machine got nothing the model could look for. These are the
    shelves the prefetch can now recognise; the live catalogue test checks
    that each one still matches something on the shelf.
    """

    @pytest.mark.parametrize(
        ("said", "shelf"),
        [
            ("enakku 30000 kulla oru AC venum", "ac"),
            ("air conditioner 35000 kulla", "ac"),
            ("washing machine 25000 kulla", "washing machine"),
            ("fridge 30000 kulla", "fridge"),
            ("headphone 5000 kulla", "headphones"),
            ("water heater 8000 kulla", "geyser"),
            ("எனக்கு இருபதாயிரம் வரை ஒரு ஃபேன்", "fan"),
        ],
    )
    def test_it_reads_the_shelf(self, said: str, shelf: str):
        intent = read_intent(said)
        assert intent is not None and intent.category == shelf

    def test_in_that_case_is_not_a_shelf(self):
        """`case` is a shelf the tool can filter on and NOT one the prefetch
        looks for, because "in that case" is a sentence."""
        assert read_category("in that case 20000 kulla phone") == "phone"

    def test_the_cheap_shelves_simply_do_not_prefetch(self):
        """A power bank is 1,500 rupees, under the budget floor.

        Deliberate, and the floor comment says why: below the floor a bare
        number is read as thousands, so lowering it to catch "2000 kulla"
        would change what that means for the phone it usually means. The
        model still searches and still answers; only the optimisation is
        skipped.
        """
        assert read_intent("power bank 2000 kulla") is None


class TestTheEnumAndTheMapAgree:
    def test_every_category_the_model_may_ask_for_can_be_filtered(self):
        """An enum value with no mapping is a filter that silently does
        nothing - the search runs unfiltered and the customer is read a
        shortlist from the wrong shelf."""

        from src.agent.tools.catalogue import CATEGORIES as FACETS
        from src.agent.tools.declarations import SEARCH_PRODUCTS

        params: Any = SEARCH_PRODUCTS["parameters"]
        offered = params["properties"]["category"]["enum"]

        missing = [name for name in offered if name not in FACETS]
        assert not missing, f"no catalogue facet for {missing}"


async def test_a_new_price_on_the_same_shelf_is_still_a_prefetch():
    """The half of the browsing case the first version missed.

    "illa, 20-30k kulla paarunga" names no product. `read_intent` returns
    nothing for it, correctly - with no history a budget alone does not say
    which shelf. But there IS history here, and the customer who has just
    been read three phones does not say "phone" again.
    """
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("30-40k kulla phone venum")
    assert pre.task is not None
    await pre.task
    pre.heard("illa, 20-30k kulla paarunga")
    assert pre.task is not None
    await pre.task

    assert len(tools.calls) == 2
    assert tools.calls[1][1]["category"] == "phone"
    assert tools.calls[1][1]["max_price"] == 30000


async def test_a_budget_with_no_history_is_still_not_a_search():
    """Only the carry-forward is new. A budget on its own, before anything
    has been searched, still says nothing about which shelf."""
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-30k kulla paarunga")

    assert pre.task is None
    assert tools.calls == []


class TestAFloorIsAlsoABudget:
    """ "40k mela" used to read as no budget at all.

    The reason written down was that "20k mela is not one we can search on",
    and a trace disproved it: asked for a laptop above 40,000 the model
    called search_products with min_price and no max, found three, and the
    prefetch - which had refused to read the same sentence - sat out the
    turn. That silent generation was Rs 0.35 of a Rs 2.05 call, seventeen
    per cent of it, heard by nobody.
    """

    @pytest.mark.parametrize(
        "said",
        [
            "40000 mela oru laptop venum",
            "laptop 40k mela",
            "above 40000 laptop",
            "எனக்கு நாற்பதாயிரம் மேல ஒரு லேப்டாப் வேணும்",
        ],
    )
    def test_it_reads_the_floor(self, said: str):
        intent = read_intent(said)
        assert intent is not None
        assert intent.category == "laptop"
        assert intent.min_price == 40000
        assert intent.max_price is None

    def test_the_search_is_asked_the_way_the_model_would_ask_it(self):
        """The prefetched answer is cached under the key the model's own
        call produces. A max_price of None in the args would miss it."""
        intent = read_intent("laptop 40k mela")
        assert intent is not None
        assert intent.as_args() == {"category": "laptop", "min_price": 40000}

    def test_a_range_still_reads_as_a_range(self):
        """ "20k to 40k mela" is somebody trailing off, not a floor."""
        intent = read_intent("20000 to 30000 kulla phone")
        assert intent is not None
        assert (intent.min_price, intent.max_price) == (20000, 30000)

    def test_mela_on_its_own_is_not_a_budget(self):
        """It is an ordinary word. Same rule as உள்ள: only ever read with a
        number immediately in front of it."""
        assert read_intent("mela poonga") is None
        assert read_intent("mela irukkura phone") is None


async def test_it_says_when_it_started_and_when_it_landed():
    """ "used: true" at the end of a call does not say whether it WON.

    The model starts generating the moment its own turn detection says the
    caller stopped. A prefetch that primes a second after that has paid for
    a search nobody used, and the call still shows `used: true` - which is
    how a trace came back saying the prefetch fired on a turn the model had
    already searched for itself. Two timestamps settle it.
    """
    noted: list[tuple[str, dict]] = []
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(
        tools, prime, on_event=lambda kind, **detail: noted.append((kind, detail))
    )

    pre.heard("20-25k kulla phone venum")
    assert pre.task is not None
    await pre.task

    kinds = [kind for kind, _ in noted]
    assert kinds == ["prefetch_started", "prefetch_primed"]
    assert noted[0][1]["category"] == "phone"
    assert noted[1][1]["products"] > 0


async def test_a_prefetch_that_finds_nothing_says_so_by_staying_quiet():
    """Started and never primed is the shape of a search that came back
    empty - and on the trace it reads as a gap, which is the point."""
    noted: list[tuple[str, dict]] = []
    tools = FakeTools({"products": []})
    said, prime = primer()
    pre = Prefetcher(
        tools, prime, on_event=lambda kind, **detail: noted.append((kind, detail))
    )

    pre.heard("20-25k kulla phone venum")
    assert pre.task is not None
    await pre.task

    assert [kind for kind, _ in noted] == ["prefetch_started"]


class TestWaitingForItInsteadOfRacingIt:
    """The change that turns a race into a decision.

    The prefetch only starts once the caller has said enough to read an
    intent from. On the trace of 2026-11-16 that was 740 ms before they
    stopped talking, against a search that needed longer - so it lost, and
    the search it paid for was thrown away while the model paid for the
    round trip anyway. No amount of making the search faster creates head
    start that is not there.

    So the caller's turn is held instead, briefly, and the model is told
    they finished once the shortlist is already in its context.
    """

    async def test_it_waits_for_a_search_still_in_flight(self):
        tools = FakeTools(FOUND)
        said, prime = primer()
        pre = Prefetcher(tools, prime)

        pre.heard("20-25k kulla phone venum")
        assert pre.task is not None and not pre.task.done()
        landed = await pre.settle(2.0)

        assert landed is True
        assert said, "the model was not primed before the turn was released"

    async def test_a_turn_with_no_prefetch_is_not_held_at_all(self):
        """Most turns. `settle` has to return at once or it sits in front of
        every reply on the call."""
        import time

        pre = Prefetcher(FakeTools(FOUND), primer()[1])

        started = time.monotonic()
        landed = await pre.settle(5.0)

        assert landed is False
        assert time.monotonic() - started < 0.05

    async def test_a_slow_prefetch_is_given_up_on(self):
        """A cap that passes means the model searches as it always did. The
        one thing this must never do is hold a caller on a dead line."""
        import time

        class Slow:
            shortlist: tuple = ()

            async def search_ahead(self, args):
                await asyncio.sleep(30)
                raise AssertionError("should not finish")  # pragma: no cover

        pre = Prefetcher(Slow(), primer()[1])
        pre.heard("20-25k kulla phone venum")

        started = time.monotonic()
        landed = await pre.settle(0.2)
        waited = time.monotonic() - started

        assert landed is False
        assert waited < 1.0, "the cap did not hold"

    async def test_giving_up_is_recorded(self):
        """So a trace shows the wait was spent and bought nothing."""
        noted: list[str] = []

        class Slow:
            shortlist: tuple = ()

            async def search_ahead(self, args):
                await asyncio.sleep(30)
                raise AssertionError("should not finish")  # pragma: no cover

        pre = Prefetcher(
            Slow(), primer()[1], on_event=lambda kind, **d: noted.append(kind)
        )
        pre.heard("20-25k kulla phone venum")
        await pre.settle(0.2)

        assert "prefetch_waited_out" in noted


@pytest.mark.parametrize("same_search", [True, False])
async def test_model_search_owns_the_shortlist_when_a_prefetch_is_still_running(
    same_search, monkeypatch
):
    from src.agent.tools.runner import ToolSession
    from src.config import load
    from src.web.services.voice_call import VoiceCall
    from tests.test_catalogue import PAYLOAD, FakeResponse, catalogue_with

    catalogue, poster = catalogue_with(FakeResponse(PAYLOAD))
    tools = ToolSession(catalogue)
    started, release = asyncio.Event(), asyncio.Event()
    lookup = tools.search_ahead

    async def slow_search(args):
        started.set()
        await release.wait()
        return await lookup(args)

    monkeypatch.setattr(tools, "search_ahead", slow_search)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    call = VoiceCall(None, load(), None)
    said, prime = primer()
    pre = Prefetcher(tools, prime)
    call._prefetch = pre
    pre.heard("25k kulla phone")
    await started.wait()
    waiting_for_turn = asyncio.create_task(pre.settle(2))
    args = {"category": "phone", "max_price": 25000}
    if not same_search:
        args["min_price"] = 20000
    request = asyncio.create_task(call._tool_runner(tools)("search_products", args))
    await asyncio.sleep(0)
    release.set()

    result = await request
    assert result["products"]
    assert len(poster.calls) == 1
    assert tools.budget == (args.get("min_price"), 25000)
    assert pre.task.cancelled() is (not same_search)
    assert not await waiting_for_turn
    assert not said, "the tool response already carries the authoritative shortlist"


async def test_a_floor_only_prefetch_delivers_its_result():
    said, prime = primer()
    pre = Prefetcher(FakeTools(FOUND), prime)
    pre.heard("20k mela phone")
    assert await pre.settle(2)
    assert "from 20,000" in said[0]
    assert "total_matching=4" in said[0]


async def test_prefetched_index_prices_keep_the_catalogue_qualification():
    result = {**FOUND, "price_note": "approximate - confirm before quoting"}
    said, prime = primer()
    pre = Prefetcher(FakeTools(result), prime)
    pre.heard("25k kulla phone")
    assert await pre.settle(2)
    assert result["price_note"] in said[0]


async def test_the_emi_for_the_top_phone_is_primed_after_the_shortlist():
    """The brain answered EMI from memory on about half the calls (2026-11-26,
    quoting ₹48 and ₹904 for a phone whose EMI was ₹652 a month). The figure
    is now primed unasked, as its own line after the shortlist, so the brain
    does not have to choose to look it up."""
    tools = FakeTools(FOUND)
    tools.emi = {
        "ref": "1",
        "name": "Poco M8 5G",
        "per_month": 652,
        "months": 48,
        "provider": "HDFC Bank Debit Card",
    }
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task
    assert pre._emi is not None
    await pre._emi

    assert said[0].startswith("search_products has already run")
    assert (
        "EMI for ref 1, Poco M8 5G: HDFC Bank Debit Card, 48 months, from 652 rupees a month."
        in said[1]
    )
    assert len(said) == 2


async def test_no_emi_means_no_extra_line():
    tools = FakeTools(FOUND)
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("20-25k kulla phone")
    assert pre.task is not None
    await pre.task
    assert pre._emi is not None
    await pre._emi

    assert len(said) == 1
