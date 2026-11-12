"""The page, the settings it needs, and whether this process is healthy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

from src.agent.persona import GREETING
from src.config import Config

STATIC = Path(__file__).resolve().parents[1] / "static"


def install(app: FastAPI, settings: Config) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Is this process alive? Nothing downstream is contacted.

        Deliberately cheap and deliberately uninformative: a load balancer
        polls this every few seconds, and a health check that calls Gemini
        would both cost money and fail the container for somebody else's
        outage.
        """
        return {"status": "ok", "env": settings.env}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        """Is this process configured well enough to take a call?

        Reports WHICH capability is missing rather than a bare false, because
        "the agent cannot search" and "the agent cannot check stock" are
        different outages with different owners. No secret is named and no
        value is echoed - only whether each one arrived.
        """
        checks = {
            "model_key": bool(settings.gemini_api_key),
            "catalogue": bool(settings.catalogue_token),
            "estate": settings.estate_backend != "off",
        }
        return {"ready": all(checks.values()), "checks": checks}

    @app.get("/api/v1/config")
    async def config_summary() -> dict[str, Any]:
        """Only what the browser needs to open its audio devices.

        Deliberately no model names, no word caps, no thinking budget. Which
        vendor and which model Poorvika runs on is commercial information,
        and a public page has no reason to publish it - this project learned
        the reference implementation's entire stack by reading exactly such a
        blob off its page. Not repeating that mistake outward.

        The key is not here and never will be: the browser talks to this
        server, and only this server talks to the model.
        """
        return {
            "capture_sample_rate": settings.capture_sample_rate,
            "playback_sample_rate": settings.playback_sample_rate,
            "greeting": GREETING,
        }
