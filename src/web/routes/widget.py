"""The embeddable chat: a static page, its loader, and a read-only history.

Nothing here calls a model. The page is cacheable and paints its welcome
from the HTML itself. History is a redraw of messages already paid for.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from src.chat.copy import PLACEHOLDER, SUBTITLE, SUGGESTIONS, welcome_line
from src.config import Config, accent_hover
from src.web.services.chat_v2 import products_from_cards

WIDGET = Path(__file__).resolve().parents[1] / "static" / "widget"
_JS = "application/javascript; charset=utf-8"
# Served in place of the loader when widget.enabled is off, so a storefront
# that still points at this URL renders no button and opens no iframe.
_DISABLED_JS = "/* Shopping Assistant is switched off. */\n"


def install(app: FastAPI, settings: Config) -> None:
    @app.get("/widget/v1", include_in_schema=False)
    @app.get("/widget/v1/", include_in_schema=False)
    async def page() -> Response:
        if not settings.widget_enabled:
            return HTMLResponse("Not found", status_code=404)
        html = (WIDGET / "index.html").read_text(encoding="utf-8")
        name = settings.brand_name or settings.assistant_name
        color = settings.brand_primary_color
        surfaces = (
            ["guided", "voice", "chat"] if settings.app_mode == "full" else ["chat"]
        )
        config = {
            "v": 1,
            "assistant_name": name,
            "subtitle": settings.brand_subtitle or SUBTITLE,
            "headline": welcome_line(name),
            "placeholder": PLACEHOLDER,
            "color": color,
            "logo_url": settings.brand_logo_url,
            "host_origins": list(settings.widget_host_origins),
            "suggestions": list(SUGGESTIONS),
            "welcome": welcome_line(name),
            "surfaces": surfaces,
            "app_mode": settings.app_mode,
        }
        logo = ""
        if settings.brand_logo_url:
            logo = (
                '<img class="sa-logo" id="sa-logo" alt="" src="'
                + escape(settings.brand_logo_url, quote=True)
                + '">'
            )
        html = (
            html.replace(
                "__SA_CONFIG_JSON__", json.dumps(config).replace("<", "\\u003c")
            )
            .replace("__SA_HEADLINE__", escape(welcome_line(name)))
            .replace("__SA_SUBTITLE__", escape(settings.brand_subtitle or SUBTITLE))
            .replace("__SA_LEAD__", escape(SUBTITLE))
            .replace("__SA_COLOR__", escape(color))
            .replace("__SA_HOVER__", escape(accent_hover(color)))
            .replace("__SA_NAME__", escape(name))
            .replace("__SA_LOGO__", logo)
            .replace("__SA_PLACEHOLDER__", escape(PLACEHOLDER))
        )
        return HTMLResponse(html)

    @app.get("/widget/v1/sa-loader.js", include_in_schema=False)
    async def loader() -> Response:
        if not settings.widget_enabled:
            return Response(_DISABLED_JS, media_type=_JS)
        return FileResponse(WIDGET / "sa-loader.js", media_type=_JS)

    @app.get("/widget/v1/widget.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(WIDGET / "widget.js", media_type=_JS)

    @app.get("/widget/v1/widget.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(WIDGET / "widget.css", media_type="text/css; charset=utf-8")

    @app.get("/api/v1/widget/history")
    async def history(request: Request) -> dict[str, object]:
        """Redraw a conversation. No model, no reservation, no socket."""
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        host = request.headers.get("x-sa-host", "")
        if not token or not host:
            return {"messages": [], "context_label": "this page"}
        resumed = await request.app.state.conversations.resume(token, host)
        if resumed is None:
            return {"messages": [], "context_label": "this page"}
        conv, rotated = resumed
        return {
            "messages": [
                {
                    "role": item.get("role"),
                    "text": item.get("text", ""),
                    "cards": item.get("cards") or [],
                    "products": item.get("products")
                    or products_from_cards(list(item.get("cards") or [])),
                    "suggestions": item.get("suggestions") or [],
                }
                for item in conv.history
                if item.get("role") in {"user", "assistant"} and item.get("text")
            ],
            "token": rotated,
            "context_label": conv.context_label,
        }
