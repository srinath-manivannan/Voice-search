"""One error shape, whichever way the request came in.

The sockets have always answered `{"type": "error", "message": ...}`. HTTP
answered FastAPI's `{"detail": ...}`, so the page had two failure shapes to
parse for the same service and the second one was never written down
anywhere. This makes them the same.

Three things each error carries, and each is here for a reason:

- **`type`**, so a client can branch on it without guessing from the status.
- **`message`**, which is for a person. A 500 says the same sentence every
  time on purpose: an exception's text is written for whoever wrote the code
  and regularly contains a URL, a payload or a token, and an error page is a
  cheap place to read one.
- **`request_id`**, so the sentence on screen and the stack trace in the log
  can be joined up by someone who has only one of them.

The status code is unchanged. What a 404 or a 422 MEANS is HTTP's business,
and inventing our own would be the opposite of rule 25.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.observability.reporting import Reporter

log = logging.getLogger(__name__)

# What a caller is told when the server failed in a way nobody anticipated.
# Deliberately identical for every such failure: see the note on `message`.
UNEXPECTED = "something went wrong on our side"


def _body(message: str, request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "type": "error",
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
        **extra,
    }


def install(app: FastAPI, reporter: Reporter) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """A 404 or a 405: the status is the information, the text is a label."""
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(str(exc.detail), request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422, with WHICH field was wrong - that part is safe to say.

        The field names and the reasons come from our own schema, not from
        what the caller sent, so echoing them tells an attacker nothing they
        did not already have and tells an honest client what to fix.
        """
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", [])),
                "problem": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_body("that request could not be read", request, fields=fields),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """500. The detail goes to the log and the reporter, never to the
        response."""
        reporter.capture(
            exc,
            where="http",
            method=request.method,
            path=request.url.path,
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=500, content=_body(UNEXPECTED, request))
