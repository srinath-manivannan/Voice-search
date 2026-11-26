"""Orange storefront, product cards, and v2 chat in both app modes."""

from __future__ import annotations

import os
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from src.config import load
from src.web.app import create_app
from tests.e2e.conftest import Stack, _free_port

pytestmark = pytest.mark.e2e


def test_orange_tokens_and_product_actions(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    page.get_by_role("button", name="Open Shopping Assistant chat").click()
    page.locator("iframe[title='Shopping Assistant chat']").wait_for(state="attached")
    frame = next(item for item in page.frames if "/widget/v1" in (item.url or ""))
    frame.locator("#sa-welcome").wait_for(state="visible")
    color = frame.locator(".sa-header").evaluate(
        "el => getComputedStyle(el).backgroundColor"
    )
    assert color == "rgb(243, 111, 33)", color
    frame.get_by_role("button", name="Is this available?").click()
    expect(frame.get_by_role("button", name="View Details")).to_be_visible(
        timeout=10000
    )
    expect(frame.get_by_role("button", name="Show Similar")).to_be_visible()
    expect(frame.get_by_role("button", name="View", exact=True)).to_be_visible()
    assert watched.errors == []


def test_direct_widget_is_a_narrow_shell(page, stack: Stack) -> None:  # noqa: ANN001
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.widget + "/widget/v1/")
    page.locator("#sa-welcome").wait_for(state="visible")
    width = page.locator("body").evaluate("el => el.getBoundingClientRect().width")
    assert width <= 420
    assert page.locator("html").get_attribute("data-standalone") == "1"


@pytest.fixture(scope="module")
def full_app() -> str:
    port = _free_port()
    keys = {
        "GEMINI_API_KEY": "test-key-not-real",
        "APP_MODE": "full",
        "CONVERSATION_TOKEN_SECRET": "test-conversation-secret",
        "WIDGET_DEMO_CATALOGUE": "1",
        "WIDGET_STUB_MODEL": "1",
        "WIDGET_STUB_DELAY_MS": "0",
        "LIVE_VOICE": "0",
        "WIDGET_ENABLED": "1",
        "CHAT_PAID_ENABLED": "1",
    }
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        settings = load("development")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    app = create_app(settings)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("full app did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True


def test_full_mode_widget_replies(page, full_app: str) -> None:  # noqa: ANN001
    page.goto(full_app + "/widget/v1/")
    page.locator("#sa-welcome").wait_for(state="visible")
    page.locator("#sa-input").fill("What is your return policy?")
    page.locator(".sa-send").click()
    expect(page.locator(".sa-msg.assistant", has_text="return window")).to_be_visible(
        timeout=15000
    )


def test_start_call_does_not_open_guided(page, full_app: str) -> None:  # noqa: ANN001
    page.goto(full_app + "/")
    page.get_by_role("tab", name="Voice Call").click()
    page.get_by_role("button", name="Start Call").click()
    expect(page.locator("#voice-panel")).to_be_visible()
    expect(page.locator("#guided-panel")).to_be_hidden()
    expect(page.locator("#voice-notice")).to_contain_text("LIVE_VOICE=1")
    expect(page.locator("#voice-notice")).to_contain_text("VOICE_ROUTE_B=1")
