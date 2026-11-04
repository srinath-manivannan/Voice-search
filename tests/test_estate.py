"""Per-item facts, and the one error a customer can see.

Every payload here is a trimmed copy of a real response, captured on
2026-11-04 from item 53952 (Vivo Y51 Pro 5G) against production and item 45902
against both environments. The shapes are not invented, because the bugs worth
testing for are all in the shapes.
"""

from __future__ import annotations

import pytest

from src.agent.tools import estate as estate_api
from src.agent.tools.runner import ToolSession

# Production, item 53952. One instant discount of Rs 2,000 on HDFC cards, one
# EMI-availability row that is not a discount at all, and EMI from Rs 992.
ANSWERED = {
    "item_code": "53952",
    "channel": "online",
    "partial": [],
    "took_ms": 970,
    "summary": {
        "mrp": 34999,
        "list_price": 34999,
        "selling_price": 34999,
        "discount_percent": None,
        "inclusive_of_taxes": True,
        "effective_price": 32999,
        "best_offer": {
            "offer_id": "431151",
            "amount": 2000,
            "label": "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards",
            "payment": {
                "card_types": ["debit", "credit"],
                "transaction_types": ["emi"],
                "gateways": ["PayU"],
            },
        },
        "offers_considered": 1,
        "basis": "the single best card offer on this channel, not a total",
        "emi_starts_from": {
            "per_month": 992,
            "months": 48,
            "provider": "HDFC Bank Debit Card",
            "type": "Standard EMI",
        },
    },
    "sections": {
        "product": {
            "status": "ok",
            "data": {"name": "Vivo Y51 Pro 5G ( Crimson Red, 8GB-128GB )"},
        },
        "pricing": {
            "status": "ok",
            "data": {
                "price": 34999,
                "mrp": 34999,
                "discount_percent": None,
                "price_list": "ONLINE",
                "hide_selling_price": False,
            },
        },
        "offers": {
            "status": "ok",
            "data": {
                "offers": [
                    {
                        "offer_id": "431151",
                        "kind": "instant_discount",
                        "label": (
                            "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards"
                        ),
                        "amount": 2000,
                        "reduces_price": False,
                        "reduces_effective": True,
                        "payment": {
                            "card_types": ["debit", "credit"],
                            "transaction_types": ["emi"],
                        },
                    },
                    {
                        "offer_id": "954755",
                        "kind": "emi_availability",
                        "label": "EMI :",
                        "amount": None,
                        "reduces_price": False,
                        "reduces_effective": False,
                        "discount": {"basis": "percentage", "value": 0},
                    },
                ]
            },
        },
        "emi": {
            "status": "ok",
            "data": {
                "headline": {
                    "per_month": 992,
                    "months": 48,
                    "provider": "HDFC Bank Debit Card",
                    "type": "Standard EMI",
                },
                "kind_count": 1,
                "provider_count": 20,
            },
        },
        # Production has no working Central Inventory credential. This is the
        # real text the estate reports for it.
        "stock": {
            "status": "not_configured",
            "reason": (
                "no ci host for production: ci-gateway.poorvika.com answers "
                "but rejects both published tokens, which are staging's"
            ),
        },
    },
}


def test_a_service_that_could_not_be_asked_is_not_an_empty_shelf():
    """The one error here a customer can see.

    Production cannot reach Central Inventory, and reading that as zero is how
    a stocked phone gets a Notify Me button. `unknown` and `none` must stay
    different all the way to the sentence.
    """
    facts = estate_api.read(ANSWERED)

    assert facts.stock.state == estate_api.UNKNOWN
    assert facts.stock.in_stock is None
    assert "rejects both published tokens" in (facts.stock.reason or "")
    assert facts.unanswered == (estate_api.STOCK,)


def test_an_unreadable_body_arriving_as_http_200_is_unknown():
    """`{"message": "signature is invalid"}` comes back with HTTP 200.

    The CI gateway verifies its token with a secret chosen by the `mode`
    header, so the same token is accepted under one mode and refused under
    another - and the refusal is a 200 with no stock figure in it.
    """
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["stock"] = {
        "status": "invalid",
        "reason": "signature is invalid",
    }

    stock = estate_api.read(payload).stock
    assert stock.state == estate_api.UNKNOWN
    assert stock.in_stock is None


def test_stock_that_answered_zero_is_out_of_stock_and_not_unknown():
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["stock"] = {
        "status": "ok",
        "data": {"in_stock": False, "quantity": 0, "branch_count": 0},
    }

    stock = estate_api.read(payload).stock
    assert (stock.state, stock.in_stock, stock.quantity) == (
        estate_api.KNOWN,
        False,
        0,
    )


def test_an_emi_availability_row_is_not_a_discount():
    """It arrives with a null amount and a 0% discount.

    Counting it as an offer is how an agent invents a saving, and it is the
    only offer on most products - so the agent would announce a discount on
    nearly everything.
    """
    offers = estate_api.read(ANSWERED).offers

    assert [o.label for o in offers.items] == [
        "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards"
    ]
    assert offers.best_amount == 2000
    assert offers.effective_price == 32999


def test_an_offer_keeps_the_card_that_makes_it_apply():
    """ "2,000 off irukku" is false. "HDFC card-la" is the whole claim."""
    offer = estate_api.read(ANSWERED).offers.items[0]

    assert offer.condition == "debit/credit card on emi"
    assert offer.lowers_price is True


def test_offers_are_alternatives_and_never_a_total():
    assert estate_api.read(ANSWERED).offers.stacks is False


def test_the_emi_headline_is_the_one_the_shop_quotes():
    """Not the smallest instalment of the three kinds.

    A No Cost plan over 6 months has the largest instalment and is the
    cheapest offer on the page, so "smallest wins" is wrong. The upstream
    picks Standard, which is what poorvika.com prints.
    """
    emi = estate_api.read(ANSWERED).emi

    assert emi.state == estate_api.KNOWN
    assert (emi.per_month, emi.months) == (992, 48)
    assert emi.provider == "HDFC Bank Debit Card"
    assert emi.kind == "Standard EMI"
    assert emi.provider_count == 20


def test_staging_reports_emi_as_unknown_rather_than_absent():
    """There is no staging twin of the shop's EMI calculator.

    Quoting production EMI against staging prices would be a real instalment
    for a price nobody is charging.
    """
    payload = {**ANSWERED, "summary": {}, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["emi"] = {
        "status": "not_configured",
        "reason": "no emi host for stage: set EMI_HOST to the confirmed one",
    }

    emi = estate_api.read(payload).emi
    assert emi.state == estate_api.UNKNOWN
    assert emi.per_month is None


def test_a_price_the_shop_hides_is_not_spoken():
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["pricing"] = {
        "status": "ok",
        "data": {"price": 34999, "mrp": 34999, "hide_selling_price": True},
    }

    price = estate_api.read(payload).price
    assert price.state == estate_api.UNKNOWN
    assert price.selling is None


def test_a_response_without_sections_is_an_outage_not_an_answer():
    with pytest.raises(estate_api.EstateUnavailable):
        estate_api.read({"error": "q is required"})


# ── the tools, end to end ─────────────────────────────────────────────────


class FakeEstate:
    """A backend with one payload per item code. No network, no money."""

    def __init__(self, by_code: dict[str, dict]) -> None:
        self._by_code = by_code
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    async def product(self, item_code: str, sections: tuple[str, ...]) -> dict:
        self.asked.append((item_code, sections))
        if item_code not in self._by_code:
            raise estate_api.EstateUnavailable(f"no payload for {item_code}")
        return self._by_code[item_code]


def priced(item_code: str, selling: int) -> dict:
    return {
        "item_code": item_code,
        "sections": {
            "pricing": {
                "status": "ok",
                "data": {"price": selling, "mrp": selling},
            }
        },
        "summary": {"selling_price": selling, "list_price": selling},
    }


def hits(*documents: dict) -> dict:
    return {
        "results": [
            {
                "found": len(documents),
                "hits": [{"document": d} for d in documents],
            }
        ]
    }


def document(item_code: str, name: str, price: int) -> dict:
    return {
        "item_code": item_code,
        "name": name,
        "price": price,
        "brand_name": "Samsung",
        "availability": "In Stock",
        "stock_status": "In Stock",
        "variant_status": 1,
    }


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakePoster:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def post(self, url, *, json=None, headers=None):  # noqa: A002
        return FakeResponse(self._payload)


def catalogue_of(*documents: dict):
    from src.agent.tools.catalogue import Catalogue

    payload = hits(*documents)
    return Catalogue(
        "https://example.invalid", "token", poster_factory=lambda: FakePoster(payload)
    )


async def test_a_phone_the_index_prices_inside_the_budget_is_dropped_if_it_is_not():
    """Measured: the index said 25,499 for a phone that sells at 35,499.

    Both its stock fields agreed and it was returned for a 30,000 budget. The
    index carries the newest MRP record rather than the price list, so a band
    filter on it offers phones a third over what was asked for.
    """
    tools = ToolSession(
        catalogue_of(
            document("52064", "Samsung Galaxy A17 5G", 25499),
            document("45902", "Vivo Y200 Pro 5G", 29999),
        ),
        estate=FakeEstate(
            {"52064": priced("52064", 35499), "45902": priced("45902", 22999)}
        ),
    )

    found = await tools.run(
        "search_products", {"category": "phone", "max_price": 30000}
    )

    names = [p["name"] for p in found["products"]]
    assert "Samsung Galaxy A17 5G" not in names
    assert names == ["Vivo Y200 Pro 5G"]
    # And the price spoken is the price list's, not the index's.
    assert found["products"][0]["price_inr"] == 22999
    assert "price_note" not in found


async def test_a_follow_up_about_stock_does_not_search_again():
    """Three of four turns in a real conversation need no discovery at all."""
    estate = FakeEstate({"45902": priced("45902", 22999), "53952": ANSWERED})
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)), estate=estate
    )

    await tools.run("search_products", {"query": "vivo"})
    answer = await tools.run("product_details", {"ref": "1", "want": ["stock", "emi"]})

    assert answer["product"].startswith("Vivo Y51 Pro 5G")
    assert answer["stock"]["state"] == estate_api.UNKNOWN
    assert answer["emi"]["from_inr_per_month"] == 992
    assert answer["could_not_check"] == [estate_api.STOCK]
    # Pricing, then the sections the two questions need - and nothing else.
    assert estate.asked[-1][1] == ("stock", "product", "pricing", "emi")


async def test_the_second_one_resolves_without_an_item_code():
    estate = FakeEstate(
        {
            "45902": priced("45902", 22999),
            "53952": priced("53952", 31999),
        }
    )
    tools = ToolSession(
        catalogue_of(
            document("45902", "Vivo Y200 Pro 5G", 29999),
            document("53952", "Vivo Y51 Pro 5G", 34999),
        ),
        estate=estate,
    )

    await tools.run("search_products", {"query": "vivo"})
    answer = await tools.run("product_details", {"ref": "2", "want": ["price"]})

    assert answer["product"] == "Vivo Y51 Pro 5G"
    assert answer["price_inr"] == 31999


async def test_a_name_the_customer_repeated_back_resolves_too():
    estate = FakeEstate({"52064": priced("52064", 25499)})
    tools = ToolSession(
        catalogue_of(document("52064", "Samsung Galaxy A17 5G", 25499)), estate=estate
    )

    await tools.run("search_products", {"query": "samsung"})
    answer = await tools.run(
        "product_details", {"ref": "that Samsung", "want": ["price"]}
    )

    assert answer["price_inr"] == 25499


async def test_an_estate_outage_is_reported_as_an_outage():
    tools = ToolSession(
        catalogue_of(document("45902", "Vivo Y200 Pro 5G", 29999)),
        estate=FakeEstate({"45902": priced("45902", 22999)}),
    )
    await tools.run("search_products", {"query": "vivo"})

    tools._estate = FakeEstate({})  # the gateway stops answering mid-call
    assert await tools.run("product_details", {"ref": "1", "want": ["emi"]}) == {
        "error": "details_unavailable"
    }


async def test_a_deployment_without_the_route_is_an_outage_not_an_answer():
    """The deployed Worker predates /product and answers the search error.

    Treated as an answer, every section would read as absent and the agent
    would tell every customer there are no offers and no EMI.
    """

    class OldWorker:
        status_code = 200

        def json(self) -> dict:
            return {"error": "q is required"}

        async def post(self, url, *, json=None, headers=None):  # noqa: A002
            return self

    backend = estate_api.WorkerEstate(
        "https://example.invalid/product", "token", client_factory=OldWorker
    )
    with pytest.raises(estate_api.EstateUnavailable, match="no /product route"):
        await backend.product("53952", ("emi",))


async def test_a_phrase_that_fits_two_products_asks_instead_of_guessing():
    """Two Vivos on the shortlist and "that Vivo" names neither.

    One short question costs a turn. Quoting EMI on the wrong phone costs the
    sale, and the customer has no way to tell it happened.
    """
    estate = FakeEstate(
        {"45902": priced("45902", 22999), "53952": priced("53952", 31999)}
    )
    tools = ToolSession(
        catalogue_of(
            document("45902", "Vivo Y200 Pro 5G", 29999),
            document("53952", "Vivo Y51 Pro 5G", 34999),
        ),
        estate=estate,
    )
    await tools.run("search_products", {"query": "vivo"})

    answer = await tools.run("product_details", {"ref": "that Vivo", "want": ["emi"]})
    assert answer["error"] == "no_such_product"
    assert answer["known_refs"] == ["1", "2"]


async def test_a_phone_over_the_budget_is_not_offered_inside_it():
    """Measured live: a 30,999 phone came back for "20,000 to 30,000".

    A 5% margin here let it through, and the agent then presented it as being
    inside the range. A salesperson may offer to stretch a budget; they do not
    do it without saying so.
    """
    tools = ToolSession(
        catalogue_of(
            document("52060", "Samsung Galaxy A17 5G W/O TA", 24499),
            document("45902", "Vivo Y200 Pro 5G", 29999),
        ),
        estate=FakeEstate(
            {"52060": priced("52060", 30999), "45902": priced("45902", 22999)}
        ),
    )

    found = await tools.run(
        "search_products", {"category": "phone", "min_price": 20000, "max_price": 30000}
    )
    assert [p["name"] for p in found["products"]] == ["Vivo Y200 Pro 5G"]


async def test_the_former_price_comes_from_the_price_list_not_the_index():
    """Pairing the two produced "was 24,499, now 30,999" - a rise.

    Read aloud after "offer irukku", that is a discount the customer can hear
    and nobody is giving.
    """
    tools = ToolSession(
        catalogue_of(document("45902", "Vivo Y200 Pro 5G", 24499)),
        estate=FakeEstate(
            {
                "45902": {
                    "item_code": "45902",
                    "sections": {
                        "pricing": {
                            "status": "ok",
                            "data": {"price": 22999, "mrp": 29999},
                        }
                    },
                    "summary": {"selling_price": 22999, "list_price": 29999},
                }
            }
        ),
    )

    product = (await tools.run("search_products", {"query": "vivo"}))["products"][0]
    assert (product["price_inr"], product["was_inr"]) == (22999, 29999)


async def test_nothing_to_report_carries_no_reason():
    """ "none" needs no explanation; "unknown" always has one."""
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["offers"] = {"status": "empty", "data": {"offers": []}}
    tools = ToolSession(
        catalogue_of(document("53952", "Vivo Y51 Pro 5G", 34999)),
        estate=FakeEstate({"53952": payload}),
    )
    await tools.run("search_products", {"query": "vivo"})

    answer = await tools.run("product_details", {"ref": "1", "want": ["offers"]})
    assert answer["offers"] == {"state": estate_api.NONE}


def test_the_struck_through_price_is_the_mrp_and_not_the_price_list_figure():
    """The summary carries both, and on a discount they are not the same.

    Item 45902: mrp 29,999, list_price 22,999, selling 22,999, 23% off. Taking
    `list_price` as the former price made every discounted phone look like it
    had no discount - the one thing a customer calling about a budget wants.
    """
    payload = {
        "item_code": "45902",
        "sections": {
            "pricing": {
                "status": "ok",
                "data": {"price": 22999, "mrp": 29999, "discount_percent": 23},
            }
        },
        "summary": {
            "mrp": 29999,
            "list_price": 22999,
            "selling_price": 22999,
            "discount_percent": 23,
        },
    }

    price = estate_api.read(payload).price
    assert (price.selling, price.list_price, price.discount_percent) == (
        22999,
        29999,
        23,
    )


async def test_a_feature_word_does_not_empty_the_shelf():
    """ "30,000 kulla camera phone" found nothing on the running server.

    The query is matched against product names, and no phone is called
    "camera" - the index states a camera on 2,397 of 12,926 products and never
    in the name. So the customer was told there are none, for one of the
    commonest questions there is.
    """
    from src.agent.tools.catalogue import Catalogue

    answers = [hits(), hits(document("45902", "Vivo Y200 Pro 5G", 29999))]

    class Changing:
        async def post(self, url, *, json=None, headers=None):  # noqa: A002
            return FakeResponse(answers.pop(0))

    tools = ToolSession(
        Catalogue("https://example.invalid", "token", poster_factory=Changing),
        estate=FakeEstate({"45902": priced("45902", 22999)}),
    )

    found = await tools.run(
        "search_products",
        {"query": "camera", "category": "phone", "max_price": 30000},
    )
    assert [p["name"] for p in found["products"]] == ["Vivo Y200 Pro 5G"]


async def test_a_name_that_matches_nothing_with_no_band_to_fall_back_on():
    """Without a category or a budget there is nothing else to search on."""
    from src.agent.tools.catalogue import Catalogue

    class Empty:
        async def post(self, url, *, json=None, headers=None):  # noqa: A002
            return FakeResponse(hits())

    tools = ToolSession(
        Catalogue("https://example.invalid", "token", poster_factory=Empty)
    )
    found = await tools.run("search_products", {"query": "nokia 3310"})
    assert found["products"] == []
