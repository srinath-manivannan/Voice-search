"""URL to handler, and nothing more.

Every module here is thin on purpose: accept the connection, hand it to a
service, return what comes back. When a route starts holding rules, the rules
belong in `src/web/services` instead.
"""

from __future__ import annotations

from fastapi import FastAPI

from src.config import Config
from src.web.routes import chat, client_errors, guided, metrics, pages, voice, widget


def install(app: FastAPI, settings: Config) -> None:
    """Register every route. One call, so nothing can be forgotten quietly.

    ``APP_MODE=widget`` mounts the embeddable chat only. Voice, Guided, the
    recordings and the shelf-warm hook stay unregistered, so a widget
    process cannot spend on them by being asked.
    """
    if settings.app_mode == "widget":
        pages.install_health(app, settings, include_warm=False)
        widget.install(app, settings)
        chat.install(app, settings)
        client_errors.install(app, settings)
        metrics.install(app, settings)
        return
    pages.install(app, settings)
    voice.install(app, settings)
    chat.install(app, settings)
    guided.install(app, settings)
    widget.install(app, settings)
    client_errors.install(app, settings)
    metrics.install(app, settings)
