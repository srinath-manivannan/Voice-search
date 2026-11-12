"""Errors the browser hit, reported from here rather than from there.

The rule asks for frontend errors as well as backend ones. The browser does
not get a DSN to do it with: a key in a page is a key anyone can read, and
this page already refuses to publish which model it runs on. So the page
posts what broke and the server reports it, through the same scrubber and
to the same place.

What is accepted is deliberately thin - a message, a source and a line. No
stack, no user agent, no page state. A stack from a browser carries whatever
was in scope, and on this page that includes the conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from src.config import Config
from src.observability.reporting import Reporter

log = logging.getLogger(__name__)


class BrowserError(BaseModel):
    """Validated and bounded, because this endpoint is open to the page.

    The lengths are not politeness: an unbounded string here is a way to
    fill a log, and a way to post a conversation to a service that promises
    not to keep one.
    """

    message: str = Field(max_length=300)
    source: str = Field(default="", max_length=200)
    line: int = Field(default=0, ge=0, le=10_000_000)


class _BrowserFailure(Exception):
    """So the reporter sees an exception, which is what it reports."""


def install(app: FastAPI, settings: Config) -> None:
    @app.post("/api/v1/client-errors", status_code=202)
    async def client_error(report: BrowserError, request: Request) -> dict[str, Any]:
        reporter: Reporter = request.app.state.reporter
        reporter.capture(
            _BrowserFailure(report.message),
            where="browser",
            source=report.source,
            line=report.line,
            request_id=getattr(request.state, "request_id", None),
        )
        return {"type": "accepted"}
