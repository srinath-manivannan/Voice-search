#!/usr/bin/env python3
"""Smoke-check a Release 1 widget process before any traffic moves.

The checks are the ones a canary has to pass while it is still the only
copy taking requests: it is alive, Voice and Guided are not mounted, the
embed can be framed by the storefront and cannot use a microphone, a
foreign page cannot open the socket, and sending the same message twice
does not produce a second answer.

    python scripts/verify_release1_canary.py \\
        --url http://127.0.0.1:8080 \\
        --host-origin http://localhost:8088

``--host-origin`` is the storefront origin on ``WIDGET_HOST_ORIGINS``.
``--allow-unready`` is for the local demo stack, which has no live
catalogue token. A rollout gate omits that flag.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

_MESSAGE = "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a10"
_STATIC_OK = "frame-ancestors"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _ws_url(base: str) -> str:
    origin = _origin(base).rstrip("/")
    if origin.startswith("https://"):
        return "wss://" + origin[len("https://") :] + "/ws/chat"
    return "ws://" + origin[len("http://") :] + "/ws/chat"


def _fetch(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as failed:
        headers = {key.lower(): value for key, value in failed.headers.items()}
        return failed.code, headers, failed.read()


def _health(base: str, timeout: float) -> str | None:
    try:
        status, _headers, body = _fetch(base.rstrip("/") + "/healthz", timeout)
    except Exception as failed:
        return f"/healthz: {failed}"
    if status != 200:
        return f"/healthz: HTTP {status}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "/healthz: not JSON"
    if payload.get("status") != "ok":
        return "/healthz: status is not ok"
    return None


def _ready(base: str, timeout: float, allow_unready: bool) -> str | None:
    try:
        status, _headers, body = _fetch(base.rstrip("/") + "/readyz", timeout)
    except Exception as failed:
        return f"/readyz: {failed}"
    if status != 200:
        return f"/readyz: HTTP {status}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "/readyz: not JSON"
    if "checks" not in payload:
        return "/readyz: no checks"
    if not allow_unready and payload.get("ready") is not True:
        return "/readyz: not ready (pass --allow-unready for a local demo)"
    return None


def _dormant(base: str, timeout: float) -> str | None:
    failures = []
    for path in ("/ws", "/api/v1/guided/start"):
        try:
            status, _headers, _body = _fetch(base.rstrip("/") + path, timeout)
        except Exception as failed:
            failures.append(f"{path}: {failed}")
            continue
        if status != 404:
            failures.append(f"{path}: HTTP {status}, expected 404")
    if failures:
        return "; ".join(failures)
    return None


def _embed_headers(base: str, timeout: float) -> str | None:
    url = base.rstrip("/") + "/widget/v1/"
    try:
        status, headers, _body = _fetch(url, timeout)
    except Exception as failed:
        return f"/widget/v1/: {failed}"
    if status != 200:
        return f"/widget/v1/: HTTP {status}"
    policy = headers.get("content-security-policy", "")
    permissions = headers.get("permissions-policy", "")
    problems = []
    if _STATIC_OK not in policy or "frame-ancestors 'none'" in policy:
        problems.append("CSP frame-ancestors does not name a host")
    for token in ("microphone=()", "camera=()", "geolocation=()"):
        if token not in permissions:
            problems.append(f"Permissions-Policy missing {token}")
    if "x-frame-options" in headers:
        problems.append("X-Frame-Options is set; the embed allowlist cannot use it")
    if problems:
        return "/widget/v1/: " + "; ".join(problems)
    return None


def _close_code(exc: ConnectionClosed) -> int | None:
    received = exc.rcvd
    if received is None:
        return None
    return int(received.code)


def _origin_refused(base: str, timeout: float) -> str | None:
    try:
        with connect(
            _ws_url(base),
            origin="https://not-this-service.example",
            proxy=None,
            open_timeout=timeout,
            close_timeout=timeout,
            ping_interval=None,
        ) as socket:
            try:
                socket.recv(timeout=timeout)
            except ConnectionClosed as closed:
                code = _close_code(closed)
                if code != 1008:
                    return f"disallowed origin closed with {code}, expected 1008"
                return None
    except ConnectionClosed as closed:
        code = _close_code(closed)
        if code != 1008:
            return f"disallowed origin closed with {code}, expected 1008"
        return None
    except Exception as failed:
        return f"disallowed origin: {failed}"
    return "disallowed origin: the socket stayed open"


def _until(socket: object, kind: str, timeout: float) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []
    recv = getattr(socket, "recv")
    for _ in range(16):
        raw = recv(timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        message = json.loads(raw)
        if not isinstance(message, dict):
            continue
        seen.append(message)
        if message.get("type") == kind:
            return seen
    raise TimeoutError(f"no {kind} in {seen}")


def _idempotent(base: str, host_origin: str, timeout: float) -> str | None:
    hello = {
        "type": "hello",
        "v": 2,
        "token": None,
        "last_seq": 0,
        "context": {
            "host_origin": host_origin,
            "host_session": "hs_canary",
            "pincode": "600040",
        },
    }
    user = {"type": "user_message", "message_id": _MESSAGE, "text": "hello"}
    try:
        with connect(
            _ws_url(base),
            origin=_origin(base),
            proxy=None,
            open_timeout=timeout,
            close_timeout=timeout,
            ping_interval=None,
        ) as socket:
            socket.send(json.dumps(hello))
            welcome = json.loads(socket.recv(timeout=timeout))
            if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                return f"idempotent send: expected welcome, got {welcome!r}"
            socket.send(json.dumps(user))
            first = _until(socket, "done", timeout)
            socket.send(json.dumps(user))
            second = _until(socket, "done", timeout)
    except Exception as failed:
        return f"idempotent send: {failed}"
    first_final = next((item for item in first if item.get("type") == "final"), None)
    second_final = next((item for item in second if item.get("type") == "final"), None)
    if first_final is None or second_final is None:
        return "idempotent send: a reply was missing"
    if first_final.get("display") != second_final.get("display"):
        return "idempotent send: the replay was a different reply"
    kinds = [item.get("type") for item in second]
    if "ack" in kinds:
        return "idempotent send: the replay started a new answer"
    return None


def verify(
    base_url: str,
    host_origin: str,
    *,
    allow_unready: bool = False,
    timeout: float = 5.0,
) -> list[str]:
    """Return a list of failures. An empty list means the canary passed."""
    checks = (
        _health(base_url, timeout),
        _ready(base_url, timeout, allow_unready),
        _dormant(base_url, timeout),
        _embed_headers(base_url, timeout),
        _origin_refused(base_url, timeout),
        _idempotent(base_url, host_origin, timeout),
    )
    return [item for item in checks if item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Widget process, no trailing path")
    parser.add_argument(
        "--host-origin",
        required=True,
        help="Storefront origin listed in WIDGET_HOST_ORIGINS",
    )
    parser.add_argument(
        "--allow-unready",
        action="store_true",
        help="Do not fail when /readyz reports ready=false",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    failures = verify(
        args.url,
        args.host_origin,
        allow_unready=args.allow_unready,
        timeout=args.timeout,
    )
    if failures:
        print("release 1 canary: failed", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("release 1 canary: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
