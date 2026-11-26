"""Exercise the production wire format through the widget's real shopping path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.agent.tools.catalogue import Catalogue, CatalogueUnavailable, list_price
from src.chat.live_catalogue import LiveCatalogue, PAGE_SIZE
from src.config import load
from src.web.app import create_app
from tests.test_catalogue import FakeResponse
from tests.test_widget_release1 import hello, settings


def handset(code, **fields):
    return {
        "item_code": code,
        "name": "Handset " + code,
        "variant_id": "v1",
        "special_price": 28499,
        "price": 39999,
        "mrp": [{"price": [{"price": 40999}]}],
        "stock_status": "In Stock",
        "isActive": True,
        "categories_slug": ["mobile", "smartphones"],
        **fields,
    }


def page(docs, found=None):
    return {
        "found": len(docs) if found is None else found,
        "hits": [{"document": d} for d in docs],
    }


class Proxy:
    def __init__(self, answer):
        self.answer = answer
        self.searches = []

    async def post(self, url, *, json, headers):
        (query,) = json["searches"]
        self.searches.append(query)
        assert headers["Origin"] == "bot"
        assert headers["User-Agent"] and headers["access-token"] == "example"
        return FakeResponse({"results": [self.answer(query)]})


def test_two_turn_widget_returns_production_shaped_cards(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_DEMO_CATALOGUE", "0")
    monkeypatch.setenv("CATALOGUE_TOKEN", "example")
    monkeypatch.setenv("CHAT_PAID_ENABLED", "1")
    proxy = Proxy(
        lambda q: page(
            [
                handset("EXP", special_price=49000),
                handset("PAISE", special_price=30000.25, price=25000),
                handset("ACC", categories_slug=["phone-holders"]),
                handset("OFF", stock_status="Out Of Stock"),
                handset("INACTIVE", isActive=False),
                handset("SHOW", name="Demo Handset"),
                handset("ONE", ram=["8GB"], internal_storage=["1TB"]),
                handset("TWO", price=None, special_price=None, selling_price=22999),
                handset(
                    "THREE",
                    price=None,
                    special_price=None,
                    prices=[{"min": [{"price": 18999}]}],
                ),
            ]
        )
    )

    monkeypatch.setattr(
        "src.web.services.dependencies.catalogue_for",
        lambda _: Catalogue(
            "https://example.invalid/search", "example", poster_factory=lambda: proxy
        ),
    )
    with (
        TestClient(create_app(settings(monkeypatch))) as client,
        client.websocket_connect(
            "/ws/chat?v=2", headers={"origin": "http://testserver"}
        ) as ws,
    ):
        ws.send_json(hello())
        ws.receive_json()

        def turn(text, mid):
            ws.send_json({"type": "user_message", "message_id": mid, "text": text})
            while True:
                event = ws.receive_json()
                if event["type"] == "final":
                    return event
                assert event["type"] != "error", event

        first = turn(
            "Help me find the right phone", "00000000-0000-4000-b000-000000000001"
        )
        assert "budget" in first["display"]["text"].lower()
        assert not proxy.searches
        second = turn("Under ₹30,000", "00000000-0000-4000-b000-000000000002")
    (query,) = proxy.searches
    assert second["cards"][0]["variant_label"] == "8 GB + 1024 GB"
    assert query["q"] == "*" and query["per_page"] == 40
    assert all(
        f in query["filter_by"]
        for f in (
            "special_price:>0",
            "special_price:<=30000",
            "isActive:=true",
            "stock_status:=In Stock",
            "categories_slug:=smartphones",
        )
    )
    assert {p["product_id"]: p["price_inr"] for p in second["products"]} == {
        "ONE": 28499,
        "TWO": 22999,
        "THREE": 18999,
    }


@pytest.mark.parametrize("rejected", [True, False])
async def test_unindexed_or_empty_filtered_page_retries_and_paginates(rejected):
    def answer(q):
        if q.get("filter_by"):
            if rejected:
                return {"code": 400, "error": "not a filterable field"}
            return page([])
        if q["page"] == 1:
            return page(
                [handset(str(i), special_price=34999) for i in range(PAGE_SIZE)],
                PAGE_SIZE + 1,
            )
        return page([handset("OK", special_price=None, price=24999)], PAGE_SIZE + 1)

    proxy = Proxy(answer)
    adapter = LiveCatalogue(
        Catalogue("", "example", poster_factory=lambda: proxy), None, ()
    )
    items = await adapter.search(
        category="phone", brand=None, min_price=20000, max_price=30000
    )
    assert [i.sku_id for i in items] == ["OK"]
    assert items[0].price.list_inr == 40999
    assert list_price({"mrp": 40999.0}) == 40999
    assert list_price({"mrp": 29999.5}) == 30000
    assert proxy.searches[-1]["page"] == 2


async def test_unavailable_catalogue_is_not_an_empty_shelf():
    proxy = Proxy(lambda q: {"code": 403, "error": "refused"})
    adapter = LiveCatalogue(
        Catalogue("", "example", poster_factory=lambda: proxy), None, ()
    )
    with pytest.raises(CatalogueUnavailable):
        await adapter.search(category="phone", brand=None, max_price=30000)
    assert len(proxy.searches) == 1


def test_production_env_layout_does_not_mix_estates(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setenv("TYPESENSE_API_HOST", "https://example.invalid/typesense")
    monkeypatch.setenv("TYPESENSE_API_KEY", "search-example")
    monkeypatch.setenv("TYPESENSE_COLLECTION", "catalogue_v2")
    monkeypatch.setenv("API_ENV", "production")
    monkeypatch.setenv("CI_HOST", "https://stage.example.invalid/inventory")
    monkeypatch.delenv("PROD_CI_HOST", raising=False)
    monkeypatch.setenv("PIM_ACCESS_TOKEN", "stage")
    monkeypatch.setenv("PROD_PIM_ACCESS_TOKEN", "production")
    s = load("development")
    assert s.catalogue_key_header == "X-TYPESENSE-API-KEY"
    assert (
        s.catalogue_collection == "catalogue_v2"
        and s.catalogue_token == "search-example"
    )
    assert not s.estate_secrets["CI_HOST"]
    assert s.estate_secrets["PIM_ACCESS_TOKEN"] == "production"
