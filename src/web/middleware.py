"""Cross-cutting HTTP concerns, in one place rather than in every handler.

Three of them, and each exists because of a specific failure mode:

- **A request id on every request and every log line.** A voice call produces
  dozens of log lines across several modules, and without a handle tying them
  together "which call was that?" is answered by guessing at timestamps.
  The id goes out on the response too, so a screenshot of a broken call is
  enough to find it in the log.
- **Security headers**, because the page embeds no third-party anything and
  there is no reason to let a browser be talked into treating it otherwise.
- **No caching**, because this page is edited while it runs and a cached
  `app.js` reads as "my change did nothing".

Nothing here logs a header value or a query string. Secrets travel in headers
on the way out of this process, and a middleware that prints requests is how
they end up in a log file.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Served from one origin, loading nothing from anywhere else. Stated rather
# than assumed, so a stray <script src> from a CDN fails loudly in review
# instead of quietly shipping.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
}

Handler = Callable[[Request], Awaitable[Response]]


def install(app: FastAPI) -> None:
    """Wire the middleware onto an app. Order is outermost-last in FastAPI."""

    @app.middleware("http")
    async def tag_and_protect(request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        took_ms = round((time.perf_counter() - started) * 1000)

        # Method, path, status, duration. Never the query string and never a
        # header: both carry tokens on the services this talks to, and a log
        # line is the easiest place in a system to leak one.
        log.info(
            "%s %s -> %s in %d ms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            took_ms,
            request_id,
        )

        response.headers[REQUEST_ID_HEADER] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # This is a development tool that is edited while it runs. If it is
        # ever served to real users the static mount wants proper cache
        # headers instead of this.
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
