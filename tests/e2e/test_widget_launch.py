"""Release 1 widget, exercised in Chromium and WebKit.

Run with the browsers installed:

    pytest -m e2e --browser chromium --browser webkit \\
        --override-ini="addopts=-q --strict-markers"
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e

WELCOME = "Hi! I'm Shopping Assistant."
STATIC = {
    "/widget/v1",
    "/widget/v1/",
    "/widget/v1/widget.js",
    "/widget/v1/widget.css",
    "/widget/v1/sa-loader.js",
}


def _frame(page):  # noqa: ANN001
    page.locator("iframe[title='Shopping Assistant chat']").wait_for(state="attached")
    for frame in page.frames:
        if "/widget/v1" in (frame.url or ""):
            return frame
    raise AssertionError([frame.url for frame in page.frames])


def _open(page):  # noqa: ANN001
    button = page.get_by_role("button", name="Open Shopping Assistant chat")
    button.click()
    frame = _frame(page)
    frame.locator("#sa-welcome").wait_for(state="visible")
    frame.locator("html[data-ready='1']").wait_for(state="attached")
    return button, frame


def _probe(page, key: str) -> int:  # noqa: ANN001
    total = 0
    for frame in page.frames:
        try:
            total += int(
                frame.evaluate(
                    "(name) => (window.__saProbe && window.__saProbe[name]) || 0",
                    key,
                )
            )
        except Exception:
            continue
    return total


def _widget_urls(urls: list[str], widget: str) -> list[str]:
    unexpected = []
    for url in urls:
        if not url.startswith(widget):
            continue
        path = url[len(widget) :].split("?", 1)[0]
        if path not in STATIC:
            unexpected.append(url)
    return unexpected


def _focused_label(page) -> str:  # noqa: ANN001
    return str(
        page.evaluate(
            """() => {
              const root = document.querySelector('#sa-launcher-root');
              const inner = root && root.shadowRoot && root.shadowRoot.activeElement;
              return inner ? (inner.getAttribute('aria-label') || '') : '';
            }"""
        )
    )


def test_dormant_panel_creates_no_socket_audio_or_permission(
    watched, stack: Stack, browser_name: str
) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    button = page.get_by_role("button", name="Open Shopping Assistant chat")
    for attempt in range(3):
        button.click()
        frame = _frame(page)
        # The greeting is for the first visit only; later opens go straight
        # to the conversation.
        state = "visible" if attempt == 0 else "hidden"
        frame.locator("#sa-welcome").wait_for(state=state)
        button.click()
        expect(button).to_have_attribute("aria-expanded", "false")
    gum = _probe(page, "gum")
    play = _probe(page, "play")
    audio = _probe(page, "audio")
    page.reload()
    button = page.get_by_role("button", name="Open Shopping Assistant chat")
    button.click()
    _frame(page).locator("#sa-welcome").wait_for(state="hidden")
    button.click()
    assert watched.sockets == [], browser_name
    assert _widget_urls(watched.requests, stack.widget) == [], browser_name
    assert gum + _probe(page, "gum") == 0
    assert play + _probe(page, "play") == 0
    assert audio + _probe(page, "audio") == 0
    state = page.evaluate(
        """async () => {
          try {
            const status = await navigator.permissions.query({name: 'microphone'});
            return status.state;
          } catch (err) {
            return 'unsupported';
          }
        }"""
    )
    assert state != "granted", state
    assert watched.errors == []


def test_welcome_is_local_and_immediate(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    page.get_by_role("button", name="Open Shopping Assistant chat").click()
    frame = _frame(page)
    frame.locator("#sa-welcome").wait_for(state="visible")
    timing = frame.evaluate(
        """() => {
          const nav = performance.getEntriesByType('navigation')[0];
          const text = document.getElementById('sa-welcome').textContent || '';
          return { ms: nav ? nav.domContentLoadedEventEnd : 9999, text };
        }"""
    )
    assert WELCOME in timing["text"]
    assert timing["ms"] < 100, timing
    assert watched.sockets == []
    assert _widget_urls(watched.requests, stack.widget) == []
    assert watched.errors == []


def test_desktop_panel_is_a_non_modal_dialog(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    _button, frame = _open(page)
    dialog = frame.locator("body")
    expect(dialog).to_have_attribute("role", "dialog")
    assert frame.evaluate("() => document.body.getAttribute('aria-modal')") is None
    assert page.locator("#product").evaluate("el => el.hasAttribute('inert')") is False
    frame.locator("#sa-input").focus()
    page.keyboard.press("Escape")
    expect(page.locator("iframe[title='Shopping Assistant chat']")).to_be_hidden()
    assert _focused_label(page) == "Open Shopping Assistant chat"
    assert watched.errors == []


def test_mobile_sheet_traps_focus_and_meets_touch_size(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(stack.storefront)
    button, frame = _open(page)
    expect(frame.locator("body")).to_have_attribute("role", "dialog")
    expect(frame.locator("body")).to_have_attribute("aria-modal", "true")
    assert page.locator("#product").evaluate("el => el.inert") is True
    boxes = frame.locator("button").evaluate_all(
        """els => els.filter(el => el.offsetParent !== null).map(el => {
          const box = el.getBoundingClientRect();
          return {
            w: box.width,
            h: box.height,
            name: (el.getAttribute('aria-label') || el.textContent || '').trim()
          };
        })"""
    )
    assert boxes, "the sheet rendered no buttons"
    for box in boxes:
        assert box["w"] >= 44 and box["h"] >= 44, box
    launcher = button.bounding_box()
    assert launcher is not None
    assert launcher["width"] >= 44 and launcher["height"] >= 44
    frame.locator(".sa-send").focus()
    page.keyboard.press("Tab")
    assert frame.locator("#sa-min").evaluate("el => document.activeElement === el")
    frame.locator("#sa-min").focus()
    page.keyboard.press("Shift+Tab")
    assert frame.locator(".sa-send").evaluate("el => document.activeElement === el")
    page.keyboard.press("Escape")
    assert _focused_label(page) == "Open Shopping Assistant chat"
    assert page.locator("#product").evaluate("el => el.hasAttribute('inert')") is False
    button.click()
    frame = _frame(page)
    frame.locator("html[data-ready='1']").wait_for(state="attached")
    frame.locator("#sa-close").click()
    assert _focused_label(page) == "Open Shopping Assistant chat"
    assert watched.errors == []


def test_bad_messages_and_foreign_origins_are_ignored(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    page.locator("#evil").wait_for(state="attached")
    page.wait_for_timeout(300)
    assert page.evaluate("() => sessionStorage.getItem('sa_conv')") in (None, "")
    _button, frame = _open(page)
    nonce = page.evaluate("() => window.__saNonce")
    assert nonce
    frame.evaluate(
        """(nonce) => {
          window.dispatchEvent(new MessageEvent('message', {
            data: {type: 'sa.init', v: 1, nonce: 'evil', product: {product_id: 'SKU-DEMO-A1'}},
            origin: 'https://not-this-service.example',
            source: window.parent
          }));
          window.dispatchEvent(new MessageEvent('message', {
            data: {type: 'sa.context', v: 1, nonce, product: {product_id: 'bad id'}},
            origin: 'https://not-this-service.example',
            source: window.parent
          }));
        }""",
        nonce,
    )
    page.evaluate(
        """(nonce) => {
          const frame = document.querySelector('#sa-launcher-root').shadowRoot.querySelector('iframe');
          const origin = new URL(frame.src).origin;
          const bad = [
            null,
            'nope',
            {type: 'sa.init', v: 2, nonce},
            {type: 'sa.context', v: 1, nonce, extra: true, product: {product_id: 'SKU-DEMO-A1'}},
            {type: 'sa.context', v: 1, nonce, product: {product_id: 'bad id', variant_id: 'VAR-1'}},
            {type: 'sa.context', v: 1, nonce, product: {product_id: '<script>', variant_id: 'x'}},
            {type: 'sa.nope', v: 1, nonce}
          ];
          bad.forEach(payload => frame.contentWindow.postMessage(payload, origin));
        }""",
        nonce,
    )
    expect(frame.locator("#sa-welcome")).to_be_visible()
    expect(frame.locator("#sa-context")).to_have_count(0)
    page.evaluate(
        """(nonce) => {
          const frame = document.querySelector('#sa-launcher-root').shadowRoot.querySelector('iframe');
          frame.contentWindow.postMessage({
            type: 'sa.context', v: 1, nonce,
            product: {product_id: 'SKU-NOT-A-PRODUCT', variant_id: 'VAR-128-BLK'}
          }, new URL(frame.src).origin);
        }""",
        nonce,
    )
    frame.get_by_role("button", name="Is this available?").click()
    expect(frame.locator("#sa-log")).to_contain_text("couldn't match", timeout=10000)
    assert frame.locator("#sa-input").is_enabled()
    assert watched.errors == []


def test_host_storage_restores_the_conversation_after_iframe_storage_is_cleared(
    watched, stack: Stack
) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    _button, frame = _open(page)
    frame.locator("#sa-input").fill("hello")
    frame.locator(".sa-send").click()
    expect(frame.locator("#sa-log")).to_contain_text(
        "Tell me what you need", timeout=10000
    )
    token = page.evaluate("() => sessionStorage.getItem('sa_conv')")
    assert token
    frame.evaluate("() => { sessionStorage.clear(); localStorage.clear(); }")
    opened = len(watched.sockets)
    page.reload()
    _button, frame = _open(page)
    expect(frame.locator(".sa-msg.user")).to_have_text("hello", timeout=10000)
    assert len(watched.sockets) == opened
    restored = page.evaluate("() => sessionStorage.getItem('sa_conv')")
    assert restored
    assert watched.errors == []


def test_a_double_tap_opens_one_socket_and_one_reply(watched, stack: Stack) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    _button, frame = _open(page)
    frame.locator("#sa-input").fill("What is your return policy?")
    frame.locator(".sa-send").dblclick()
    reply = frame.locator(".sa-msg.assistant", has_text="return window")
    expect(reply).to_have_count(1, timeout=15000)
    assert frame.locator(".sa-msg.user").count() == 1
    assert len(watched.sockets) == 1
    assert frame.locator("#sa-input").is_enabled()
    assert watched.errors == []


def test_dropping_the_socket_mid_reply_does_not_duplicate_it(
    watched, stack: Stack
) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    _button, frame = _open(page)
    frame.locator("#sa-input").fill("What is your return policy?")
    frame.locator(".sa-send").click()
    expect(frame.locator("#sa-status")).to_contain_text("Checking", timeout=5000)
    frame.evaluate(
        "() => { const list = window.__saProbe.sockets; list[list.length - 1].close(); }"
    )
    reply = frame.locator(".sa-msg.assistant", has_text="return window")
    expect(reply).to_have_count(1, timeout=15000)
    assert frame.locator(".sa-msg.user").count() == 1
    assert frame.locator("#sa-input").is_enabled()
    assert watched.errors == []


def test_availability_follows_the_variant_and_back_returns_to_the_product(
    watched, stack: Stack
) -> None:
    page = watched.page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(stack.storefront)
    button, frame = _open(page)
    frame.get_by_role("button", name="Is this available?").click()
    expect(frame.locator("#sa-log")).to_contain_text("out of stock", timeout=10000)
    page.locator("#variant").select_option("VAR-128-BLK")
    frame.locator("html[data-variant='VAR-128-BLK']").wait_for(state="attached")
    frame.locator("#sa-input").fill("Is this available?")
    frame.locator(".sa-send").click()
    expect(frame.locator(".sa-msg.assistant").last).to_contain_text(
        "8 GB + 128 GB, Black is in stock", timeout=10000
    )
    frame.get_by_role("button", name="Close chat").click()
    expect(page.locator("iframe[title='Shopping Assistant chat']")).to_be_hidden()
    assert _focused_label(page) == "Open Shopping Assistant chat"
    assert button.get_attribute("aria-expanded") == "false"
    assert watched.errors == []
