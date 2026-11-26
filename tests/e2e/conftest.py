"""A widget process and a small storefront for the browser suite.

The storefront is a different origin from the widget, which is the
relationship a real host page has. An extra origin posts junk messages so
the suite can show they are ignored.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import uvicorn

from src.config import load
from src.web.app import create_app

PROBE = """
(() => {
  const probe = window.__saProbe = { gum: 0, play: 0, audio: 0, sockets: [] };
  const devices = navigator.mediaDevices;
  if (devices && typeof devices.getUserMedia === "function") {
    const orig = devices.getUserMedia.bind(devices);
    devices.getUserMedia = function () {
      probe.gum += 1;
      return orig.apply(devices, arguments);
    };
  }
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    probe.play += 1;
    return origPlay.apply(this, arguments);
  };
  function wrap(name) {
    const Ctor = window[name];
    if (typeof Ctor !== "function") return;
    const Wrapped = function () {
      probe.audio += 1;
      return new Ctor();
    };
    Wrapped.prototype = Ctor.prototype;
    window[name] = Wrapped;
  }
  wrap("AudioContext");
  wrap("webkitAudioContext");
  const Orig = window.WebSocket;
  function WS(url, protocols) {
    const sock = protocols === undefined ? new Orig(url) : new Orig(url, protocols);
    probe.sockets.push(sock);
    return sock;
  }
  WS.prototype = Orig.prototype;
  WS.CONNECTING = Orig.CONNECTING;
  WS.OPEN = Orig.OPEN;
  WS.CLOSING = Orig.CLOSING;
  WS.CLOSED = Orig.CLOSED;
  window.WebSocket = WS;
})();
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(body: bytes) -> ThreadingHTTPServer:
    payload = body

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _host_page(widget: str, evil: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Demo storefront</title>
</head>
<body>
  <main id="product">
    <h1>Demo Handset A1</h1>
    <p>Illustrative sample. Not a live offer.</p>
    <label for="variant">Variant</label>
    <select id="variant">
      <option value="VAR-256-BLK" selected>8 GB + 256 GB, Black</option>
      <option value="VAR-128-BLK">8 GB + 128 GB, Black</option>
    </select>
  </main>
  <iframe id="evil" title="Other origin" src="{evil}/" hidden></iframe>
  <script>
    window.__saNonce = "";
    window.__saNav = null;
    window.addEventListener("message", function (event) {{
      if (event.data && event.data.type === "sa.ready") window.__saNonce = event.data.nonce || "";
    }});
    document.getElementById("variant").addEventListener("change", function (event) {{
      var loader = document.querySelector("script[data-sa-origin]");
      if (loader) loader.setAttribute("data-variant-id", event.target.value);
    }});
    document.addEventListener("sa-navigate", function (event) {{
      window.__saNav = event.detail || null;
      var product = document.getElementById("product");
      if (product && product.scrollIntoView) product.scrollIntoView();
    }});
  </script>
  <script
    src="{widget}/widget/v1/sa-loader.js"
    data-sa-origin="{widget}"
    data-product-id="SKU-DEMO-A1"
    data-variant-id="VAR-256-BLK"
    data-pincode="600040"></script>
</body>
</html>
"""
    return html.encode()


def _evil_page() -> bytes:
    return b"""<!DOCTYPE html>
<meta charset="utf-8"><title>Other origin</title>
<body><script>
parent.postMessage({type:"sa.init", v:1, nonce:"hijack",
  product:{product_id:"SKU-DEMO-A1", variant_id:"VAR-128-BLK"}}, "*");
parent.postMessage({type:"sa.persist", v:1, token:"stolen-token", nonce:"hijack"}, "*");
parent.postMessage({type:"sa.context", v:1, nonce:"hijack",
  product:{product_id:"not valid", variant_id:"x"}}, "*");
</script></body>
"""


def _settings(port: int, store_origin: str):
    keys = {
        "GEMINI_API_KEY": "test-key-not-real",
        "APP_MODE": "widget",
        "WIDGET_HOST_ORIGINS": store_origin,
        "WIDGET_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
        "CONVERSATION_TOKEN_SECRET": "test-conversation-secret",
        "WIDGET_DEMO_CATALOGUE": "1",
        "WIDGET_STUB_MODEL": "1",
        "WIDGET_STUB_DELAY_MS": "800",
        "WIDGET_ENABLED": "1",
        "CHAT_PAID_ENABLED": "1",
        "ESTATE_BACKEND": "off",
    }
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        return load("development")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class Stack:
    widget: str
    storefront: str
    evil: str


@pytest.fixture(scope="session")
def stack() -> Stack:
    widget_port = _free_port()
    widget = f"http://127.0.0.1:{widget_port}"
    evil_server = _serve(_evil_page())
    evil = f"http://127.0.0.1:{evil_server.server_address[1]}"
    store_server = _serve(_host_page(widget, evil))
    store_origin = f"http://127.0.0.1:{store_server.server_address[1]}"
    settings = _settings(widget_port, store_origin)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=widget_port,
        log_level="warning",
        access_log=False,
    )
    widget_server = uvicorn.Server(config)
    thread = threading.Thread(target=widget_server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not widget_server.started:
        if time.time() > deadline:
            raise RuntimeError("widget server did not start")
        time.sleep(0.05)
    try:
        yield Stack(widget=widget, storefront=store_origin, evil=evil)
    finally:
        widget_server.should_exit = True
        store_server.shutdown()
        evil_server.shutdown()


@pytest.fixture
def watched(page):  # noqa: ANN001
    page.context.add_init_script(PROBE)
    sockets: list[str] = []
    errors: list[str] = []
    page.on("websocket", lambda item: sockets.append(item.url))
    page.on("pageerror", lambda err: errors.append(str(err)))
    requests: list[str] = []
    page.on("request", lambda item: requests.append(item.url))
    return SimpleNamespace(page=page, sockets=sockets, errors=errors, requests=requests)
