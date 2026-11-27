"""Launch-kit checks that do not need a browser."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.chat.shopper import ShopperGuide, Slots
from src.chat.stub_model import STUB_REPLY
from src.config import load
from src.tools.neutrality_scan import collect, optional_tools, scan_images
from src.web.app import create_app
from tests.raster_text import blank, paint

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://www.shop.example"
MESSAGE = "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a10"


def _load_canary():
    path = ROOT / "scripts" / "verify_release1_canary.py"
    spec = importlib.util.spec_from_file_location("verify_release1_canary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(monkeypatch, **env: str):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_HOST_ORIGINS", HOST)
    monkeypatch.setenv("WIDGET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load("development")


def _hello(product: dict[str, str] | None = None) -> dict[str, object]:
    context: dict[str, object] = {
        "host_origin": HOST,
        "host_session": "hs_shopper_1",
        "pincode": "600040",
    }
    if product is not None:
        context["product"] = product
    return {"type": "hello", "v": 2, "token": None, "last_seq": 0, "context": context}


def _until(ws, kind: str) -> list[dict]:  # noqa: ANN001
    seen = []
    for _ in range(12):
        message = ws.receive_json()
        seen.append(message)
        if message.get("type") == kind:
            return seen
    raise AssertionError(seen)


def test_widget_enabled_off_hides_the_loader_and_refuses_the_socket(monkeypatch):
    app = create_app(_settings(monkeypatch, WIDGET_ENABLED="0"))
    with TestClient(app) as client:
        page = client.get("/widget/v1/")
        assert page.status_code == 404
        assert page.headers["cache-control"] == "no-store"
        loader = client.get("/widget/v1/sa-loader.js")
        assert loader.status_code == 200
        assert "Open Shopping Assistant" not in loader.text
        assert loader.headers["cache-control"] == "no-store"
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(
                "/ws/chat", headers={"origin": "http://testserver"}
            ):
                pass
    assert refused.value.code == 1013


def test_the_stub_answers_once_when_the_same_message_is_sent_again(monkeypatch):
    app = create_app(_settings(monkeypatch, WIDGET_STUB_MODEL="1"))
    model = app.state.widget_model
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(_hello())
            ws.receive_json()
            body = {
                "type": "user_message",
                "message_id": MESSAGE,
                "text": "What is your return policy?",
            }
            ws.send_json(body)
            final = next(item for item in _until(ws, "done") if item["type"] == "final")
            assert final["display"]["text"] == STUB_REPLY
            ws.send_json(body)
            again = _until(ws, "done")
            assert [item["type"] for item in again] == ["final", "done"]
            assert again[0]["display"]["text"] == STUB_REPLY
    assert model.calls == 1


def test_the_demo_catalogue_answers_availability_for_both_colours(monkeypatch):
    app = create_app(_settings(monkeypatch, WIDGET_DEMO_CATALOGUE="1"))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(
                _hello({"product_id": "SKU-DEMO-A1", "variant_id": "VAR-256-BLK"})
            )
            ws.receive_json()
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "Is this available?",
                }
            )
            final = next(item for item in _until(ws, "done") if item["type"] == "final")
    text = final["display"]["text"]
    assert "out of stock" in text.lower()
    assert "8 GB + 128 GB, Black" in text
    guide = ShopperGuide(app.state.widget_catalogue, pincode="600040")
    other = asyncio.run(
        guide.respond(
            Slots(),
            "Is this available?",
            {"product_id": "SKU-DEMO-A1", "variant_id": "VAR-128-BLK"},
        )
    )
    assert "in stock at 3 stores" in other.text.lower()


def test_ocr_reads_a_painted_token_and_ignores_a_blank(tmp_path, monkeypatch):
    if not any(note.startswith("ran: tesseract") for note in optional_tools(tmp_path)):
        pytest.skip("tesseract is not installed")
    marked = tmp_path / "tests" / "note.png"
    marked.parent.mkdir()
    marked.write_bytes(paint("NORTHSTAR"))
    hits = scan_images(tmp_path, ("northstar",))
    assert any("northstar" in hit for hit in hits), hits
    clean = tmp_path / "plain"
    clean.mkdir()
    (clean / "swatch.png").write_bytes(blank())
    assert scan_images(clean, ("northstar",)) == []
    monkeypatch.delenv("NEUTRALITY_DENYLIST", raising=False)
    assert not any("ocr " in hit for hit in collect(ROOT, ("northstar",)))


def _serve(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    return server


def test_the_canary_script_passes_against_a_widget_process(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_HOST_ORIGINS", HOST)
    monkeypatch.setenv("WIDGET_PUBLIC_ORIGIN", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    monkeypatch.setenv("CATALOGUE_URL", "https://catalogue.example/search")
    monkeypatch.setenv("CATALOGUE_TOKEN", "present")
    monkeypatch.setenv("ESTATE_BACKEND", "worker")
    app = create_app(load("development"))
    server = _serve(app, port)
    canary = _load_canary()
    try:
        failures = canary.verify(
            f"http://127.0.0.1:{port}",
            HOST,
            timeout=5,
        )
    finally:
        server.should_exit = True
    assert failures == []


def test_the_canary_script_reports_a_closed_port():
    canary = _load_canary()
    failures = canary.verify(
        "http://127.0.0.1:1",
        HOST,
        allow_unready=True,
        timeout=0.4,
    )
    assert failures
    assert any("healthz" in item for item in failures)
