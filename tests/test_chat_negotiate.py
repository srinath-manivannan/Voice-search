"""Full-app /ws/chat speaks protocol v2 when the caller asks for it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.web.app import create_app

HOST = "http://127.0.0.1:8080"
MESSAGE = "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a10"


def _app(monkeypatch, **env: str):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "full")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    monkeypatch.setenv("WIDGET_DEMO_CATALOGUE", "1")
    monkeypatch.setenv("WIDGET_STUB_MODEL", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return create_app(load("development"))


def _hello() -> dict:
    return {
        "type": "hello",
        "v": 2,
        "token": None,
        "last_seq": 0,
        "context": {"host_origin": HOST, "host_session": "hs_shopper_1"},
    }


def _until(ws, kind: str) -> list[dict]:
    seen: list[dict] = []
    for _ in range(8):
        message = ws.receive_json()
        seen.append(message)
        if message.get("type") == kind:
            return seen
    raise AssertionError(seen)


def test_query_v2_receives_a_welcome_and_a_product(monkeypatch):
    app = _app(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat?v=2") as ws:
            ws.send_json(_hello())
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "phone under ₹20,000",
                }
            )
            final = next(
                item for item in _until(ws, "final") if item["type"] == "final"
            )
    assert final["products"]
    product = final["products"][0]
    assert product["title"]
    assert product["price_label"].startswith("₹")
    assert "product_id" in product


def test_a_hello_frame_negotiates_v2_without_the_query(monkeypatch):
    app = _app(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json(_hello())
            welcome = ws.receive_json()
    assert welcome["type"] == "welcome"


def test_a_legacy_caller_still_receives_ready(monkeypatch):
    app = _app(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat") as ws:
            ready = ws.receive_json()
    assert ready["type"] == "ready"
    assert ready["greeting"]


def test_widget_mode_still_refuses_a_foreign_origin(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("WIDGET_HOST_ORIGINS", "https://shop.example")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    app = create_app(load("development"))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat?v=2", headers={"origin": "https://evil.example"}
        ) as ws:
            with pytest.raises(Exception) as refused:
                ws.receive_text()
    assert getattr(refused.value, "code", None) == 1008
