"""URL to handler, and nothing more.

Every module here is thin on purpose: accept the connection, hand it to a
service, return what comes back. When a route starts holding rules, the rules
belong in `src/web/services` instead.
"""

from __future__ import annotations

from fastapi import FastAPI

from src.config import Config
from src.web.routes import chat, client_errors, pages, voice


def install(app: FastAPI, settings: Config) -> None:
    """Register every route. One call, so nothing can be forgotten quietly."""
    pages.install(app, settings)
    voice.install(app, settings)
    chat.install(app, settings)
    client_errors.install(app, settings)
