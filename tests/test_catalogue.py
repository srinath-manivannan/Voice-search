"""The catalogue client.

The assertions that matter here are about what it refuses to claim. The Search
Blocks Worker's own comments say stock can be wrong even when both of its
fields agree, and EMI, exchange offers and store locations are absent from the
catalogue entirely - so an agent that states any of them is inventing.
"""

from __future__ import annotations

import pytest

from src.agent.tools.catalogue import (
    MAX_QUERY_CHARS,
    Catalogue,
    CatalogueUnavailable,
)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


class FakePoster:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


PAYLOAD = {
    "total": 42,
    "understood": {"price_max": 20000},
    "hits": [
        {"document": {"name": "Galaxy M35 5G", "price": 18999, "brand": "Samsung",
                      "item_code": "SM-M35", "stock_status": "In Stock"}},
        {"document": {"name": "Poco X7", "price": 19499, "brand": "Poco",
                      "item_code": "PX7", "stock_status": "Out Of Stock"}},
        {"document": {"name": "Redmi Note 14", "price": 16999, "brand": "Redmi",
                      "item_code": "RN14", "stock_status": "In Stock"}},
        {"document": {"name": "Should not be spoken", "price": 100, "item_code": "X"}},
    ],
}


def catalogue_with(response):
    poster = FakePoster(response)
    return Catalogue("https://search.example.com", "tok", lambda: poster), poster


async def test_the_token_travels_as_a_header():
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search("budget phone")
    assert poster.calls[0]["headers"]["X-Search-Token"] == "tok"
    assert "tok" not in poster.calls[0]["url"]


async def test_results_are_capped_at_what_can_be_read_aloud():
    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    result = await cat.search("budget phone")
    # Four came back; a spoken reply cannot carry four.
    assert len(result.products) == 3
    assert result.total == 42


async def test_stock_is_a_hint_and_only_when_the_field_says_so():
    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    products = (await cat.search("budget phone")).products

    assert products[0].stock_hint is True
    assert products[1].stock_hint is False
    # The Worker's own note: even agreement can lag the shop. Nothing in this
    # client should ever be read as a promise, which is why it is named a hint.
    assert not hasattr(products[0], "in_stock")


async def test_a_missing_price_is_absent_rather_than_zero():
    cat, _ = catalogue_with(FakeResponse({"hits": [{"document": {"name": "Thing"}}], "total": 1}))
    product = (await cat.search("thing")).products[0]
    assert product.price is None
    assert "price not listed" in product.spoken()


async def test_queries_are_truncated_to_what_the_worker_accepts():
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search("x" * 400)
    assert len(poster.calls[0]["json"]["q"]) <= MAX_QUERY_CHARS


async def test_a_budget_is_folded_into_the_query_the_worker_parses():
    cat, poster = catalogue_with(FakeResponse(PAYLOAD))
    await cat.search("phone", max_price=20000)
    assert "under 20000" in poster.calls[0]["json"]["q"]


async def test_an_outage_is_distinguishable_from_finding_nothing():
    """Different things to tell a customer, so different results in code."""
    cat, _ = catalogue_with(FakeResponse({}, status=503))
    with pytest.raises(CatalogueUnavailable):
        await cat.search("phone")

    cat, _ = catalogue_with(FakeResponse({"hits": [], "total": 0}))
    empty = await cat.search("nonexistent")
    assert empty.found_nothing and empty.total == 0


async def test_a_transport_failure_is_not_an_empty_shelf():
    cat, _ = catalogue_with(TimeoutError("connection timed out"))
    with pytest.raises(CatalogueUnavailable):
        await cat.search("phone")


def test_there_is_no_tool_for_what_the_catalogue_does_not_hold():
    """EMI, exchange offers and store locations are absent from the schema.

    Checked across the Search Blocks source: zero hits for exchange, finance,
    no-cost, outlet, location and pincode; every 'emi' hit is 'emitted' and
    every 'store' hit is a data store. A tool here would have to invent them.
    """
    import src.agent.tools.catalogue as module

    exported = {name.lower() for name in dir(module) if not name.startswith("_")}
    for absent in ("emi", "exchange", "storelocator", "finance", "offers"):
        assert not any(absent in name for name in exported)
