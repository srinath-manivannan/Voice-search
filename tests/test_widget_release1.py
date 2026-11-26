"""Release 1: widget mode, protocol v2, spend, neutrality, and the shopper guide."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.chat.allowlist import (
    PriceFact,
    Specs,
    StockFact,
    guard_display,
    is_english_display,
    sanitise_tool_result,
    stock_label,
)
from src.chat.copy import LIMIT_REACHED, WELCOME
from src.chat.gemini_text import ChatTurn
from src.chat.session import ChatSession
from src.chat.shopper import ShopperGuide, Slots
from src.config import load
from src.tools.neutrality_scan import collect, scan_widget
from src.web.app import create_app
from src.web.services.chat_v2 import client_ip
from src.web.services.conversation_store import Caps, MemoryStore

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://www.shop.example"
MESSAGE = "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a10"


def settings(monkeypatch, **env: str):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_HOST_ORIGINS", HOST)
    monkeypatch.setenv("WIDGET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    monkeypatch.setenv("LIVE_VOICE", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load("development")


def connect(client: TestClient):
    return client.websocket_connect("/ws/chat", headers={"origin": "http://testserver"})


def hello(token: str | None = None, last_seq: int = 0) -> dict:
    return {
        "type": "hello",
        "v": 2,
        "token": token,
        "last_seq": last_seq,
        "context": {
            "host_origin": HOST,
            "host_session": "hs_shopper_1",
            "pincode": "600040",
        },
    }


class Shelf:
    def __init__(self, items: list) -> None:
        self.items = items

    async def search(self, *, brand: str | None, max_price: int | None) -> list:
        return list(self.items)

    async def lookup(self, product_id: str, variant_id: str):
        for item in self.items:
            if item.sku_id == product_id and (
                not variant_id or item.variant_id == variant_id
            ):
                return item
        return None


def phone(name: str, price: int, sku: str, **kwargs):
    from src.chat.allowlist import ModelProduct

    stock = kwargs.pop(
        "stock", StockFact("in_stock", 3, "nearby", "2026-11-26T12:10:07+05:30")
    )
    specs = kwargs.pop(
        "specs",
        Specs(
            ois=True,
            video_max="4K",
            usb="usb_c",
            ram_gb=8,
            storage_gb=128,
            colour="Black",
        ),
    )
    return ModelProduct(
        ref="?",
        sku_id=sku,
        variant_id=kwargs.pop("variant_id", "V-1"),
        brand=kwargs.pop("brand", "Samsung"),
        model_name=name,
        model_number=kwargs.pop("model_number", name),
        variant_label=kwargs.pop("variant_label", "8 GB + 128 GB, Black"),
        category="phone",
        price=PriceFact(price, price + 1000, "known", "2026-11-26T12:10:06+05:30"),
        stock=stock,
        specs=specs,
        offer=kwargs.get("offer"),
        emi=kwargs.get("emi"),
    )


# --- dormant mode -----------------------------------------------------------


def test_widget_mode_forces_voice_off_and_mounts_only_the_widget(monkeypatch):
    made = settings(monkeypatch)
    assert made.app_mode == "widget"
    assert made.live_voice_enabled is False
    assert made.assistant_name == "Shopping Assistant"

    import httpx

    calls: list[str] = []
    real_sync = httpx.Client.request
    real_async = httpx.AsyncClient.request

    def wrap_sync(self, method, url, *args, **kwargs):  # noqa: ANN001
        calls.append(str(url))
        return real_sync(self, method, url, *args, **kwargs)

    async def wrap_async(self, method, url, *args, **kwargs):  # noqa: ANN001
        calls.append(str(url))
        return await real_async(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", wrap_sync)
    monkeypatch.setattr(httpx.AsyncClient, "request", wrap_async)

    app = create_app(made)
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/ws" not in paths
    assert "/ws/chat" in paths
    assert not any(path.startswith("/api/v1/guided") for path in paths)
    assert not any(path.startswith("/guided") for path in paths)
    assert "/static" not in paths

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/widget/v1/").status_code == 200
        assert client.get("/api/v1/guided/start").status_code == 404
        ready = client.get("/readyz").json()
        assert "warm" not in ready
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pass
    upstream = [
        url
        for url in calls
        if "testserver" not in url
        and "127.0.0.1" not in url
        and not url.startswith("/")
    ]
    assert upstream == []


def test_widget_csp_allowlists_the_host_and_denies_the_microphone(monkeypatch):
    app = create_app(settings(monkeypatch))
    with TestClient(app) as client:
        page = client.get("/widget/v1/")
        policy = page.headers["content-security-policy"]
        assert f"frame-ancestors {HOST}" in policy
        assert "x-frame-options" not in {key.lower() for key in page.headers}
        assert (
            page.headers["permissions-policy"]
            == "microphone=(), camera=(), geolocation=()"
        )
        assert WELCOME in page.text
        health = client.get("/healthz")
        assert health.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
        loader = client.get("/widget/v1/sa-loader.js").text
        widget = client.get("/widget/v1/widget.js").text
    for banned in (
        "WebSocket",
        "fetch(",
        "getUserMedia",
        "AudioContext",
        "new Audio",
        "speechSynthesis",
    ):
        assert banned not in loader
    for banned in ("getUserMedia", "AudioContext", "new Audio", "speechSynthesis"):
        assert banned not in widget
    assert "56px" in loader and "#0F5257" in loader and "Ask" in loader
    assert 'role="dialog"' in page.text or "role" in widget
    assert "aria-modal" in widget
    assert "You're offline." in widget
    assert "Reconnecting…" in widget
    assert "Message not sent." in widget


def test_a_disallowed_origin_is_refused_with_1008(monkeypatch):
    app = create_app(settings(monkeypatch))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "https://evil.example"}
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as refused:
                ws.receive_text()
    assert refused.value.code == 1008


def test_client_ip_uses_the_forwarded_header_only_when_trusted():
    headers = {"x-forwarded-for": "203.0.113.8, 10.0.0.4"}
    assert client_ip(headers, "10.0.0.4", True) == "203.0.113.8"
    assert client_ip(headers, "10.0.0.4", False) == "10.0.0.4"


async def test_the_rate_limit_is_per_client_and_a_new_ip_is_a_new_window():
    store = _store()
    assert await store.allow("chat", "203.0.113.9", 1)
    assert not await store.allow("chat", "203.0.113.9", 1)
    assert await store.allow("chat", "203.0.113.10", 1)


# --- protocol ---------------------------------------------------------------


def _until(ws, kind: str) -> list[dict]:
    seen = []
    for _ in range(12):
        message = ws.receive_json()
        seen.append(message)
        if message.get("type") == kind:
            return seen
    raise AssertionError(seen)


def test_a_repeated_message_id_does_not_answer_twice(monkeypatch):
    app = create_app(settings(monkeypatch))
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.send_json(hello())
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            assert welcome["replay"] == []
            ws.send_json(
                {"type": "user_message", "message_id": MESSAGE, "text": "hello"}
            )
            first = _until(ws, "done")
            assert any(item["type"] == "final" for item in first)
            final = next(item for item in first if item["type"] == "final")
            assert final["display"]["lang"] == "en"
            assert "வ" not in final["display"]["text"]
            ws.send_json(
                {"type": "user_message", "message_id": MESSAGE, "text": "hello"}
            )
            again = _until(ws, "done")
            assert [item["type"] for item in again] == ["final", "done"]
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "a different question",
                }
            )
            reused = ws.receive_json()
            assert reused["code"] == "message_id_reused"
            token = welcome["token"]
        with connect(client) as ws:
            ws.send_json(hello(token))
            restored = ws.receive_json()
            assert any(item.get("type") == "final" for item in restored["replay"])


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, history, temperature=0.7):  # noqa: ANN001
        self.calls += 1
        turn = ChatTurn()
        if self.calls == 1:
            turn.function_call = {"name": "search_products", "args": {}}
            turn.function_call_part = {"functionCall": turn.function_call}
            turn.usage = {"promptTokenCount": 2000, "candidatesTokenCount": 20}
            yield "", turn
            return
        turn.usage = {"promptTokenCount": 2200, "candidatesTokenCount": 40}
        turn.text = "A plain English note about battery life."
        yield turn.text, turn


class ScriptedTools:
    async def run(self, name: str, args: dict) -> dict:
        return {
            "products": [
                {
                    "name": "Visit https://shop.example/NORTHSTAR for this phone",
                    "brand": "Samsung",
                    "price_inr": 19999,
                    "specs": {"usb": "usb-c", "ois": True},
                }
            ]
        }


def test_every_round_is_metered_and_a_reconnect_does_not_reset_it(monkeypatch):
    monkeypatch.setenv("NEUTRALITY_DENYLIST", "northstar")
    app = create_app(settings(monkeypatch))
    model = ScriptedModel()
    app.state.widget_model = model
    app.state.widget_tools = ScriptedTools()
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.send_json(hello())
            welcome = ws.receive_json()
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "What should I know before buying?",
                }
            )
            done = _until(ws, "done")[-1]
            assert model.calls == 2
            assert done["spend"]["conversation_inr"] > 0
            spent = done["spend"]["conversation_inr"]
        with connect(client) as ws:
            ws.send_json(hello(welcome["token"]))
            ws.receive_json()
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "What should I know before buying?",
                }
            )
            replayed = _until(ws, "done")[-1]
            assert model.calls == 2
            assert replayed["spend"]["conversation_inr"] == spent
            body = client.get(
                "/api/v1/widget/history",
                headers={
                    "Authorization": f"Bearer {welcome['token']}",
                    "X-Sa-Host": HOST,
                },
            )
            # The resume above rotated the token. History with the old token
            # still resolves until idle expiry, because the signature matches.
            assert body.status_code == 200
            assert model.calls == 2


def test_history_redraw_does_not_call_a_model(monkeypatch):
    app = create_app(settings(monkeypatch))
    model = ScriptedModel()
    app.state.widget_model = model
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.send_json(hello())
            token = ws.receive_json()["token"]
            ws.send_json(
                {"type": "user_message", "message_id": MESSAGE, "text": "hello"}
            )
            _until(ws, "done")
        response = client.get(
            "/api/v1/widget/history",
            headers={"Authorization": f"Bearer {token}", "X-Sa-Host": HOST},
        )
        assert response.status_code == 200
        assert response.json()["messages"]
        assert model.calls == 0


def test_the_cap_stops_the_model(monkeypatch):
    app = create_app(settings(monkeypatch, CHAT_SESSION_CAP_INR="0"))
    model = ScriptedModel()
    app.state.widget_model = model
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.send_json(hello())
            ws.receive_json()
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "What should I know before buying?",
                }
            )
            final = next(item for item in _until(ws, "done") if item["type"] == "final")
    assert model.calls == 0
    assert final["display"]["text"] == LIMIT_REACHED


# --- spend store ------------------------------------------------------------


def _store() -> MemoryStore:
    return MemoryStore(Caps(100, 300, 30_000), "test-conversation-secret")


async def test_reserve_is_idempotent_and_settle_releases_the_hold():
    store = _store()
    conv, _token = await store.create(
        host_origin=HOST, host_session="hs", pincode="", product=None
    )
    first = await store.reserve(conv, "client", "rid-1", 40)
    again = await store.reserve(conv, "client", "rid-1", 40)
    assert first.ok and again.ok
    assert await store.spent_paise(conv) == 0
    settled = await store.settle("rid-1", 25)
    assert settled.ok and not settled.overrun
    assert await store.spent_paise(conv) == 25
    await store.settle("rid-1", 25)
    assert await store.spent_paise(conv) == 25


async def test_a_reservation_over_the_cap_does_not_land_and_overrun_is_counted():
    store = _store()
    conv, _token = await store.create(
        host_origin=HOST, host_session="", pincode="", product=None
    )
    refused = await store.reserve(conv, "client", "too-big", 101)
    assert not refused.ok and refused.reason == "conv"
    other, _token = await store.create(
        host_origin=HOST, host_session="same", pincode="", product=None
    )
    sibling, _token = await store.create(
        host_origin=HOST, host_session="same", pincode="", product=None
    )
    assert (await store.reserve(other, "c", "a", 80)).ok
    blocked = await store.reserve(sibling, "c", "b", 30)
    assert not blocked.ok
    overrun = await store.settle("a", 90)
    assert overrun.overrun
    assert store.overruns == 1


async def test_release_returns_the_reservation():
    store = _store()
    conv, _token = await store.create(
        host_origin=HOST, host_session="", pincode="", product=None
    )
    assert (await store.reserve(conv, "c", "rid", 50)).ok
    await store.release("rid")
    assert (await store.reserve(conv, "c", "rid-2", 50)).ok


async def test_a_message_id_is_claimed_once():
    store = _store()
    first = await store.claim_message("c1", MESSAGE, "hello")
    second = await store.claim_message("c1", MESSAGE, "hello")
    other = await store.claim_message("c1", MESSAGE, "different")
    assert first.status == "accepted"
    assert second.status == "in_progress"
    await store.finish_message("c1", MESSAGE, 4)
    third = await store.claim_message("c1", MESSAGE, "hello")
    assert third.status == "done"
    assert other.status == "reused"


# --- neutrality and the guide ----------------------------------------------


def test_poisoned_tool_results_cannot_reach_the_model():
    cleaned = sanitise_tool_result(
        {
            "products": [
                {
                    "name": "Galaxy NORTHSTAR https://evil.example/p",
                    "brand": "NotAMaker",
                    "description": "sold on northstar.example",
                    "price_inr": 18000,
                    "stock": {
                        "state": "unknown",
                        "reason": "timeout talking to northstar",
                    },
                    "url": "https://evil.example/p",
                }
            ],
            "nearest_above_budget": {"name": "secret"},
        },
        ("northstar",),
    )
    blob = json.dumps(cleaned).casefold()
    assert "northstar" not in blob
    assert "http" not in blob
    assert "reason" not in blob
    assert "description" not in blob
    assert "url" not in blob
    assert cleaned["products"][0]["stock"]["state"] == "unconfirmed"
    assert cleaned["products"][0]["brand"] == ""


def test_an_upstream_error_string_becomes_an_enum():
    cleaned = sanitise_tool_result(
        {"error": "dial tcp northstar.example:443: connection refused"},
        ("northstar",),
    )
    assert cleaned == {"error": "details_unavailable"}


def test_unknown_stock_is_not_called_out_of_stock():
    assert stock_label(StockFact("unconfirmed", None, "unknown", "")) == (
        "Stock not confirmed right now"
    )
    assert (
        "out of stock"
        not in stock_label(StockFact("unconfirmed", None, "unknown", "")).lower()
    )
    assert stock_label(StockFact("out_of_stock", 0, "nearby", "")) == "Out of stock"
    assert stock_label(StockFact("in_stock", 0, "online", "")) == "Available online"
    assert "In stock at 3 stores" in stock_label(StockFact("in_stock", 3, "nearby", ""))


def test_the_output_filter_replaces_a_deny_list_hit_and_tamil_script():
    shown, blocked = guard_display(
        "See NORTHSTAR today.", ("northstar",), "Here is the phone."
    )
    assert blocked and shown == "Here is the phone."
    tamil, blocked_ta = guard_display(
        "வணக்கம், the phone is ready.", (), "Here is the phone."
    )
    assert blocked_ta and "வ" not in tamil
    assert is_english_display("The Galaxy A36 is in stock.", ("Galaxy A36",))


def _phones():
    return [
        phone("Galaxy A36 5G", 24499, "SKU-A", variant_id="V-A"),
        phone(
            "Galaxy A26 5G",
            22999,
            "SKU-B",
            variant_id="V-B",
            stock=StockFact("unconfirmed", None, "unknown", "t"),
        ),
        phone(
            "Galaxy M36 5G",
            19999,
            "SKU-C",
            variant_id="V-C",
            specs=Specs(
                ois=True,
                video_max="4K",
                usb="usb_c",
                ram_gb=6,
                storage_gb=128,
                colour="Green",
            ),
            variant_label="6 GB + 128 GB, Green",
            stock=StockFact("in_stock", 0, "online", "t"),
        ),
        phone(
            "Galaxy F36 5G",
            18499,
            "SKU-D",
            variant_id="V-D",
            stock=StockFact("out_of_stock", 0, "nearby", "t"),
        ),
        phone("Galaxy A17 5G", 26999, "SKU-OVER", variant_id="V-O"),
        phone("Vivo V40 5G", 26500, "SKU-V3", brand="Vivo", variant_id="V-V3"),
        phone(
            "Vivo T4 5G",
            21999,
            "SKU-V1",
            brand="Vivo",
            variant_id="V-V1",
        ),
        phone(
            "Vivo Y39 5G",
            18999,
            "SKU-V2",
            brand="Vivo",
            variant_id="V-V2",
            specs=Specs(ois=False, usb="usb_c", ram_gb=8, storage_gb=128),
        ),
    ]


async def test_the_guide_keeps_budget_and_asks_before_relaxing_it():
    guide = ShopperGuide(Shelf(_phones()), pincode="600040")
    slots = Slots()
    first = await guide.respond(
        slots, "I need a Samsung phone under ₹25,000 for site videos"
    )
    assert "walking" in first.text.lower()
    assert first.cards == []
    assert "samsung" in first.text.lower()
    assert "₹25,000" in first.text
    assert "வ" not in first.text
    second = await guide.respond(
        Slots.from_dict(first.slots), "Yes I walk around a lot. Also it must have USB-C"
    )
    prices = [card["price"]["selling_inr"] for card in second.cards]
    assert prices
    assert all(price <= 25000 for price in prices)
    labels = [card["stock"]["label"] for card in second.cards]
    assert any(label == "Stock not confirmed right now" for label in labels)
    assert any(label == "Out of stock near 600040" for label in labels)
    assert any(label == "Available online" for label in labels)
    confirmed = [label for label in labels if label != "Out of stock near 600040"]
    assert all("out of stock" not in label.lower() for label in confirmed)
    third = await guide.respond(
        Slots.from_dict(second.slots), "What about Vivo instead?"
    )
    assert third.slots["budget"] == 25000
    assert third.slots["budget_hard"] is True
    assert "ois" in third.slots["needs"] and "usb_c" in third.slots["needs"]
    assert all(card["price"]["selling_inr"] <= 25000 for card in third.cards)
    refused = await guide.respond(Slots.from_dict(third.slots), "None of these")
    assert refused.cards == []
    assert all(
        "26,999" not in refused.text and "26999" not in refused.text for _ in [0]
    )
    assert refused.slots["pending"] == "relax"
    agreed = await guide.respond(
        Slots.from_dict(refused.slots), "Show Vivo up to ₹27,000"
    )
    assert any(card["price"]["selling_inr"] > 25000 for card in agreed.cards)


async def test_option_c_and_ten_distinct_models():
    guide = ShopperGuide(Shelf(_phones()), pincode="600040")
    shown = await guide.respond(
        Slots(budget=30000, budget_hard=True, brand="Samsung"), "show all"
    )
    # The shelf has four Samsung under 30k plus one over... all five Samsung are under 30000
    # except we only have 5 samsung and one is 26999. Distinct models.
    assert len(shown.cards) <= 10
    refs = [card["ref"] for card in shown.cards]
    assert refs == list("ABCDEFGHIJ")[: len(refs)]
    asked = await guide.respond(Slots.from_dict(shown.slots), "What about option C?")
    assert shown.cards[2]["model_name"] in asked.text
    many = [
        phone(f"Phone {index}", 10000 + index, f"SKU-{index}", model_number=f"M{index}")
        for index in range(12)
    ]
    # Two colour variants of the same model should collapse.
    many.append(
        phone(
            "Phone 0",
            10000,
            "SKU-DUP",
            model_number="M0",
            variant_label="8 GB + 128 GB, Blue",
            specs=Specs(ois=True, usb="usb_c", ram_gb=8, storage_gb=128, colour="Blue"),
        )
    )
    capped = await guide.respond(
        Slots(budget=50000, budget_hard=True, brand="Samsung"),
        "show me all the options",
    )
    # Use the many shelf.
    capped = await ShopperGuide(Shelf(many)).respond(
        Slots(budget=50000, budget_hard=True, brand="Samsung"),
        "show me all the options",
    )
    assert len(capped.cards) == 10
    assert "top 10" in capped.text.lower()


async def test_unconfirmed_availability_is_not_out_of_stock():
    item = phone(
        "Galaxy A36 5G",
        24499,
        "SKU-PH-10482",
        variant_id="V-8-256-BLK",
        stock=StockFact("unconfirmed", None, "unknown", "t"),
    )
    guide = ShopperGuide(Shelf([item]), pincode="600040")
    turn = await guide.respond(
        Slots(),
        "Is this available?",
        {"product_id": "SKU-PH-10482", "variant_id": "V-8-256-BLK"},
    )
    assert "out of stock" not in turn.text.lower()
    assert (
        "couldn't confirm" in turn.text.lower() or "not confirmed" in turn.text.lower()
    )
    missing = await guide.respond(
        Slots(),
        "Is this available?",
        {"product_id": "nope value!", "variant_id": "V-1"},
    )
    assert missing.context_invalid
    assert missing.context_label == "this page"


async def test_tool_rounds_record_usage_for_each_generation():
    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, history, temperature=0.7):  # noqa: ANN001
            self.calls += 1
            turn = ChatTurn()
            turn.usage = {
                "promptTokenCount": 10 * self.calls,
                "candidatesTokenCount": self.calls,
            }
            if self.calls == 1:
                turn.function_call = {"name": "search_products", "args": {}}
                turn.function_call_part = {"functionCall": turn.function_call}
                yield "", turn
                return
            yield "Done.", turn

    class Tools:
        async def run(self, name: str, args: dict) -> dict:
            return {"error": "catalogue_unavailable", "host": "https://secret.example"}

    session = ChatSession(
        Model(), tools=Tools(), sanitise=lambda result: sanitise_tool_result(result, ())
    )
    async for _ in session.send("phones"):
        pass
    assert len(session.result.turns) == 2
    blob = json.dumps(session.history)
    assert "secret.example" not in blob
    assert "catalogue_unavailable" in blob


def test_neutrality_scan_flags_a_token_and_the_widget_stays_clean(
    tmp_path, monkeypatch
):
    sample = tmp_path / "src"
    sample.mkdir()
    (sample / "note.txt").write_text("hello NORTHSTAR shop", encoding="utf-8")
    widget = tmp_path / "src" / "web" / "static" / "widget"
    widget.mkdir(parents=True)
    (widget / "sa-loader.js").write_text("button", encoding="utf-8")
    (widget / "widget.js").write_text("new WebSocket(url)", encoding="utf-8")
    hits = collect(tmp_path, ("northstar",))
    assert any("northstar" in hit for hit in hits)
    assert scan_widget(ROOT / "src" / "web" / "static" / "widget") == []
    poisoned = widget / "widget.js"
    poisoned.write_text("navigator.mediaDevices.getUserMedia()", encoding="utf-8")
    assert any("getUserMedia" in hit for hit in scan_widget(widget))


def test_widget_instruction_is_english_only():
    from src.agent.persona import widget_instruction

    text = widget_instruction("Shopping Assistant", 100)
    assert "NEVER REPLY IN ENGLISH" not in text
    assert "English" in text
    assert "retailer" in text.lower()
