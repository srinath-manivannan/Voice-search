"""The application, assembled.

One page and two WebSockets. The socket carries raw PCM both ways plus JSON
for anything the screen needs; `src/voice/web_transport.py` explains why
those are kept separate.

This exists because a conversation you cannot see is hard to judge. The CLI
reports latency after the fact; this shows which side is speaking, what the
agent said, and what each turn cost while it happens - which is what the
Phase 1 measurement run actually needs.

Nothing in this package knows about Gemini. It wires a transport to a
session, the same way the Phase 2 telephony entry point will.

The file is deliberately short. It names the pieces and the order they are
installed in, and holds no rule of its own:

    middleware  request id, security headers, no caching
    errors      one error shape, whichever way the request came in
    reporting   where a failure goes, once it has been scrubbed
    routes      URL to handler
    services    what the handlers actually do
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import Config, load
from src.observability.reporting import build as build_reporter
from src.web import errors, middleware, routes
from src.web.routes.pages import STATIC

log = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> FastAPI:
    load_dotenv()
    settings = config or load()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Poorvika voice agent",
        version="1.0.0",
        # The schema describes one internal tool. Publishing it from a public
        # host advertises the shape of every endpoint for nothing in return.
        openapi_url="/openapi.json" if settings.env != "main" else None,
    )

    # Named origins only. "*" with credentials is rejected by browsers anyway,
    # and with a socket that carries a live conversation the right list is the
    # one the deployment states - empty refuses every cross-origin page, which
    # is the safe way to be misconfigured.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "x-request-id"],
    )
    # Built once, and it is also what teaches the scrubber which strings are
    # secrets. Off unless SENTRY_DSN is set, in which case reports go to the
    # log as well - never instead.
    reporter = build_reporter(settings)
    app.state.reporter = reporter

    middleware.install(app)
    errors.install(app, reporter)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    routes.install(app, settings)
    return app
