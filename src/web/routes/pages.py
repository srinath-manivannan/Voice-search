"""The page, the settings it needs, and whether this process is healthy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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

        Deliberately cheap: a load balancer polls this every few seconds,
        and a health check that calls Gemini would both cost money and fail
        the container for somebody else's outage.

        It does carry the release, because the first question of every
        rollback is "what is actually running right now?" and answering it
        from a deployment log is how a rollback gets done twice.
        """
        return {"status": "ok", "env": settings.env, "release": settings.release}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
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
        # Warm: the phone shelf is checked against the estate and held, so a
        # recommendation will not wait on the 630-item build. A deploy can
        # hold traffic until this is true.
        flow = getattr(request.app.state, "guided", None)
        warm = bool(flow is not None and flow.warm_enough())
        return {"ready": all(checks.values()), "checks": checks, "warm": warm}

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
            # Whether the Voice Call tab may open the paid Live voice. When
            # not, the tab starts the free Guided conversation with the mic on.
            "live_voice": settings.live_voice_enabled,
            # Whether a Poorvika-hosted speech worker exists. Without one the
            # Guided mic is offered only where the browser can hear itself.
            "hosted_asr": bool(settings.guided_asr_url),
        }
