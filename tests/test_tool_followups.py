"""Follow-up questions, and not asking upstream twice for them.

Every payload here is a trimmed copy of a real response, captured on
2026-11-04 from item 53952 (Vivo Y51 Pro 5G) against production and item 45902
against both environments. The shapes are not invented, because the bugs worth
testing for are all in the shapes.
"""

from __future__ import annotations


from src.agent.tools.declarations import PRODUCT_DETAILS
from src.agent.tools.runner import ToolSession
from tests.payloads import ANSWERED
from tests.fakes import (
    FakeEstate,
    catalogue_of,
    document,
    priced,
)


# --- not asking twice -------------------------------------------------------
#
# Every tool call costs a whole extra generation of the model - one turn that
# says nothing to make the call, another to answer - and under the Live API's
# billing each re-processes the entire context window. On a measured call that
# was five silent generations out of twenty-four. These are the round trips
# that no longer happen.


async def test_the_same_search_twice_does_not_go_upstream_twice():
    """The model asked for the same phone three times in one real call.

    Twice with identical arguments in a different key order, nine seconds
    apart. The second one is now answered from the first.
    """
    estate = FakeEstate({"45902": priced("45902", 22999)})
    catalogue = catalogue_of(document("45902", "Vivo Y200 Pro 5G", 22999))
    tools = ToolSession(catalogue, estate=estate)

    first = await tools.run("search_products", {"category": "phone", "query": "vivo"})
    upstream_after_first = len(estate.asked)
    # Same search, keys the other way round - which is how it actually arrived.
    again = await tools.run("search_products", {"query": "Vivo", "category": "phone"})

    assert again == first
    assert len(estate.asked) == upstream_after_first


async def test_a_repeated_search_does_not_renumber_the_shortlist():
    """The fault this fixes is worse than the cost.

    Rebuilding the shortlist under a customer who has already been told "the
    first one" and "the second one" quietly moves which phone those mean. A
    re-search must not change what the customer is pointing at.
    """
    estate = FakeEstate(
        {"45902": priced("45902", 22999), "52064": priced("52064", 25499)}
    )
    tools = ToolSession(
        catalogue_of(
            document("45902", "Vivo Y200 Pro 5G", 22999),
            document("52064", "Samsung Galaxy A17 5G", 25499),
        ),
        estate=estate,
    )

    await tools.run("search_products", {"query": "phone"})
    second_before = tools._shortlist[1].item_code
    await tools.run("search_products", {"query": "phone"})

    assert tools._shortlist[1].item_code == second_before


async def test_a_different_search_still_goes_and_looks():
    """The memo must not turn into a session that only ever searches once."""
    estate = FakeEstate({"45902": priced("45902", 22999)})
    tools = ToolSession(
        catalogue_of(document("45902", "Vivo Y200 Pro 5G", 22999)), estate=estate
    )

    await tools.run("search_products", {"query": "vivo"})
    upstream = len(estate.asked)
    await tools.run("search_products", {"query": "samsung", "max_price": 30000})

    assert len(estate.asked) > upstream


async def test_stock_rides_along_with_the_price_so_asking_costs_nothing():
    """ "Stock irukka?" is the commonest follow-up there is.

    The estate answers a list of sections in one request, so fetching stock
    with the price is the same round trip - and it turns that question into
    one the agent can already answer, saving a generation of the model.
    """
    estate = FakeEstate({"53952": ANSWERED})
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)), estate=estate
    )

    found = await tools.run("search_products", {"query": "vivo"})
    upstream = len(estate.asked)
    product = found["products"][0]
    # The search result already carries it.
    assert "stock" in product
    assert product["best_offer"]["amount_inr"] == 2000

    # So asking costs no request at all.
    answer = await tools.run("product_details", {"ref": "1", "want": ["stock"]})
    assert len(estate.asked) == upstream
    assert answer["stock"]["state"] == found["products"][0]["stock"]["state"]


async def test_a_section_nobody_asked_for_is_never_reported_as_unknown():
    """The trap in caching a partial answer.

    "unknown" is what the agent is told means "nobody could check", and it is
    licensed to say so to a customer. A Facts fetched for price alone would
    report EMI as unknown, and the agent would volunteer that it could not
    check something nobody asked about. So the cache records which sections
    were actually requested, and a question outside them still goes and asks.
    """
    estate = FakeEstate({"53952": ANSWERED})
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)), estate=estate
    )

    await tools.run("search_products", {"query": "vivo"})
    upstream = len(estate.asked)
    answer = await tools.run("product_details", {"ref": "1", "want": ["emi"]})

    # EMI was not prefetched, so it was fetched - not answered as "unknown".
    assert len(estate.asked) > upstream
    assert answer["emi"]["from_inr_per_month"] == 992


async def test_the_model_cannot_ask_product_details_for_what_search_already_sent():
    """The waste was the CALL, not the round trip.

    Across 22 traces the model called product_details three times and one of
    them asked for offers. The runner answered that out of its cache without
    going upstream - and it still cost a whole generation, because the call
    itself is a turn and a turn re-bills the entire context window.

    Prose had already asked it not to. So the value is gone from the enum: the
    model is no longer told that `offers` is something this tool accepts.
    """
    want = PRODUCT_DETAILS["parameters"]["properties"]["want"]
    assert want["items"]["enum"] == ["emi"]

    # The runner stays tolerant on purpose. A model that sends one of the old
    # values anyway gets an answer rather than an error - it is simply never
    # invited to.
    estate = FakeEstate({"53952": ANSWERED})
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)), estate=estate
    )
    await tools.run("search_products", {"query": "vivo"})
    answer = await tools.run("product_details", {"ref": "1", "want": ["offers"]})
    assert "error" not in answer


async def test_a_prefetch_is_shallow_because_it_is_racing_the_model():
    """These two tests asserted the opposite, and the trace overruled them.

    The prefetch used to ask for the EMI chain, on the reasoning that it
    runs "while the caller is still talking, so there is no wait to spend".
    There is: it is racing the model's own tool call, and on 2026-11-16 it
    lost by a second and a half -

        24512  prefetch_started   phone, 20-30k
        25252  end_of_speech      the caller stopped
        26786  tool_call          the model searched for itself
        28321  prefetch_primed    1,535 ms too late

    3,809 ms to prime against the 1,534 ms the model needed. The EMI chain
    spent the difference and bought a round trip the model then paid for
    anyway, so the prefetch cost money instead of saving it.

    The trade is now the other way round and it is the right way round: a
    customer who asks about EMI pays for one product_details, sometimes; a
    customer who asks for a search pays for an extra generation, always.

    If traces start showing EMI round trips, the answer is to prime shallow
    and deepen the cache behind it - not to make the prefetch slow again.
    """
    estate = FakeEstate({"53952": ANSWERED})
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)), estate=estate
    )

    ahead = await tools.search_ahead({"category": "phone", "max_price": 40000})

    assert "emi" not in estate.asked[0][1], "the EMI chain is what lost the race"
    assert ahead["products"][0]["price_inr"] == 34999, "the price still arrives"


async def test_returning_to_a_cached_budget_restores_its_refs_and_needs_no_lookup():
    estate = FakeEstate(
        {"45902": priced("45902", 22999), "53952": priced("53952", 31999)}
    )
    tools = ToolSession(
        catalogue_of(
            document("53952", "Vivo Y51 Pro", 31999),
            document("45902", "Vivo Y200 Pro", 22999),
        ),
        estate=estate,
    )
    narrower = {"category": "phone", "max_price": 25000}
    first = await tools.run("search_products", narrower)
    await tools.run("search_products", {"category": "phone", "max_price": 35000})
    second = await tools.run("product_details", {"ref": "1", "want": ["price"]})
    assert second["product"] == "Vivo Y51 Pro"
    lookups = len(estate.asked)

    assert await tools.run("search_products", narrower) == first
    assert tools.discussed is None
    restored = await tools.run("product_details", {"ref": "1", "want": ["price"]})
    assert restored["product"] == first["products"][0]["name"]
    assert tools.budget == (None, 25000)
    assert len(estate.asked) == lookups
