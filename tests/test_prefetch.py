"""Reading a search out of a half-finished sentence.

The phrasings here are the ones the project has actually seen: the worked
example in persona.py, the handover note measured on a rotated socket, and the
arguments the model passed on the traced call of 2026-11-13.

Most of these assert that NOTHING is returned. That is the point of the
module: it is called on every fragment of partial transcription and the right
answer is almost always "not yet".
"""

from __future__ import annotations

from typing import Any

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

    def test_a_floor_without_a_ceiling_is_not_searchable(self):
        assert read_intent("20k mela phone venum") is None

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

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


FOUND = {
    "total_matching": 4,
    "products": [
        {"ref": "1", "name": "Poco M8 5G", "price_inr": 19999},
        {"ref": "2", "name": "Realme P4 Lite 5G", "price_inr": 20999},
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
            "search_products",
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


async def test_it_never_rebuilds_a_shortlist_the_customer_has_been_told():
    """The one outcome worse than not prefetching.

    Once "the second one" means something to the customer, a second search
    repoints those refs at other phones and the agent starts answering about
    a product nobody mentioned.
    """
    tools = FakeTools(FOUND, shortlist=("already", "searched"))
    said, prime = primer()
    pre = Prefetcher(tools, prime)

    pre.heard("30k kulla laptop venum")

    assert pre.task is None
    assert tools.calls == []


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
