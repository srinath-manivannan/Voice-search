"""Product search against the Product List API.

Every assertion here comes from probing the real endpoint, not from the
documentation. The three that matter:

  cat_anc_codes is not a filterable field - the schema returns a 400 saying
  so - which is why category filtering goes through `categories`.

  The two stock fields disagree, and Search Blocks checked them against the
  shop: stock_status right on 13 of 13 sampled, availability on none. With
  both required plus variant_status, the 20,000-30,000 band returns 8 phones.
  Without them it returns hundreds, most unbuyable.

  A rejected query comes back 200 with an error inside the result, not as an
  HTTP error, so it has to be checked for explicitly or it reads as an empty
  shelf.
"""

from __future__ import annotations

import pytest

from src.agent.tools.catalogue import Catalogue, CatalogueUnavailable


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


class FakePoster:
    def __init__(self, response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def hit(name, price, code, avail="In Stock", stock="In Stock", brand="Samsung"):
    return {
        "document": {
            "name": name,
            "price": price,
            "item_code": code,
            "availability": avail,
            "stock_status": stock,
            "brand_name": brand,
        }
    }


PAYLOAD = {
    "results": [
        {
            "found": 8,
            "hits": [
                hit(
                    "Vivo Y200 Pro 5G ( Silky Black, 8GB-128GB )",
                    29999,
                    "45902",
                    brand="Vivo",
                ),
                hit(
                    "Realme 14 Pro Lite 5G ( Glass Gold,8GB-128GB )",
                    25999,
                    "48652",
                    brand="Realme",
                ),
                hit(
                    "Poco M8 5G ( Carbon Black, 6GB-128GB )",
                    21999,
                    "52330",
                    brand="Poco",
                ),
                hit("A fourth that must not be read aloud", 20100, "99999"),
            ],
        }
    ]
}


def catalogue_with(response):
    poster = FakePoster(response)
    return Catalogue("https://proxy.example/x", "tok", "bot", lambda: poster), poster


async def test_the_token_travels_in_a_header_not_the_url():
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search(category="phone", max_price=30000)

    call = poster.calls[0]
    assert call["headers"]["access-token"] == "tok"
    assert call["headers"]["Origin"] == "bot"
    assert "tok" not in call["url"]


async def test_category_filters_on_categories_not_cat_anc_codes():
    """The schema rejects cat_anc_codes outright with a 400."""
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search(category="phone")

    filter_by = poster.calls[0]["json"]["searches"][0]["filter_by"]
    assert "categories:=Mobiles" in filter_by
    assert "cat_anc_codes" not in filter_by


async def test_both_stock_fields_and_the_variant_must_agree():
    """Either field alone offers products the shop cannot sell."""
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search(category="phone", max_price=30000)

    filter_by = poster.calls[0]["json"]["searches"][0]["filter_by"]
    assert "availability:=`In Stock`" in filter_by
    assert "stock_status:=`In Stock`" in filter_by
    assert "variant_status:=1" in filter_by


async def test_a_budget_becomes_a_price_filter_and_sorts_dearest_first():
    """Someone asking for 'under 30,000' wants the most it will buy."""
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search(category="phone", min_price=20000, max_price=30000)

    search = poster.calls[0]["json"]["searches"][0]
    assert "price:>=20000" in search["filter_by"]
    assert "price:<=30000" in search["filter_by"]
    assert search["sort_by"] == "price:desc"


async def test_the_shortlist_is_bounded_to_what_can_be_spoken():
    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    found = await cat.search(category="phone")

    assert len(found.products) == 3
    assert found.total == 8
    assert "must not be read aloud" not in " ".join(p.name for p in found.products)


async def test_stock_is_true_only_when_both_fields_say_so():
    disagreeing = {
        "results": [
            {
                "found": 1,
                "hits": [
                    hit(
                        "Disagreeing phone",
                        24999,
                        "1",
                        avail="In Stock",
                        stock="Out Of Stock",
                    )
                ],
            }
        ]
    }
    cat, _ = catalogue_with(FakeResponse(disagreeing))
    product = (await cat.search()).products[0]
    assert product.in_stock is False


async def test_a_rejected_query_is_an_error_not_an_empty_shelf():
    """The proxy answers 200 with the error inside the result."""
    rejected = {
        "results": [{"code": 400, "error": "Could not find a filter field named `x`"}]
    }
    cat, _ = catalogue_with(FakeResponse(rejected))
    with pytest.raises(CatalogueUnavailable, match="rejected"):
        await cat.search(category="phone")


async def test_an_outage_is_distinguishable_from_finding_nothing():
    cat, _ = catalogue_with(TimeoutError("connection timed out"))
    with pytest.raises(CatalogueUnavailable):
        await cat.search()

    cat, _ = catalogue_with(FakeResponse({"results": [{"found": 0, "hits": []}]}))
    empty = await cat.search()
    assert empty.found_nothing and empty.total == 0


def test_what_is_spoken_carries_no_item_code():
    from src.agent.tools.catalogue import Product

    spoken = Product("52330", "Poco M8 5G", 21999, "Poco", True).spoken()
    assert "52330" not in spoken
    assert "₹21,999" in spoken


async def test_both_surfaces_run_the_same_tool_implementation():
    """Two copies would drift, and the drift would be two different answers."""
    from src.agent.tools.runner import run_tool

    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    result = await run_tool(
        cat, "search_products", {"category": "phone", "max_price": 30000}
    )

    assert result["total_matching"] == 8
    assert len(result["products"]) == 3
    # Named a hint everywhere it is exposed, so it cannot be read as a promise.
    assert "in_stock_hint" in result["products"][0]


async def test_an_unknown_tool_is_refused_rather_than_guessed_at():
    from src.agent.tools.runner import run_tool

    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    assert await run_tool(cat, "check_emi", {}) == {"error": "unknown_tool"}


async def test_an_outage_reaches_the_model_as_an_outage():
    """Not as an empty shelf, which the agent would report as 'nothing found'."""
    from src.agent.tools.runner import run_tool

    cat, _ = catalogue_with(TimeoutError("down"))
    assert await run_tool(cat, "search_products", {}) == {
        "error": "catalogue_unavailable"
    }
