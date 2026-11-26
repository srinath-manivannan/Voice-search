"""The embeddable chat: a static page, its loader, and a read-only history.

Nothing here calls a model. The page is cacheable and paints its welcome
from the HTML itself. History is a redraw of messages already paid for.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from src.chat.copy import SUGGESTIONS, WELCOME
from src.config import Config

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
        config = {
            "v": 1,
            "assistant_name": settings.assistant_name,
            "host_origins": list(settings.widget_host_origins),
            "suggestions": list(SUGGESTIONS),
            "welcome": WELCOME,
        }
        html = html.replace("__SA_CONFIG_JSON__", json.dumps(config))
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
                    "suggestions": item.get("suggestions") or [],
                }
                for item in conv.history
                if item.get("role") in {"user", "assistant"} and item.get("text")
            ],
            "token": rotated,
            "context_label": conv.context_label,
        }
