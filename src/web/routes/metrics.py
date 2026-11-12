"""GET /metrics - what a monitoring system scrapes.

Counters and histograms only. No transcript, no phone number, no item code,
and no label that identifies a caller: a label is a cardinality decision as
well as a privacy one, and one per caller would be both.

Behind a token when `METRICS_TOKEN` is set, which production should. The
numbers are not secrets but they are commercial - how many calls Poorvika
takes and what each costs is not for a public URL.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from src.config import Config
from src.observability.metrics import METRICS, resources

PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"


def install(app: FastAPI, settings: Config) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        if settings.metrics_token:
            offered = request.headers.get("authorization", "")
            # Compared whole, and the header is never logged or echoed.
            if offered != f"Bearer {settings.metrics_token}":
                raise HTTPException(status_code=404, detail="Not Found")
        # Read now rather than sampled on a timer: a gauge that is only
        # as fresh as a background task is a gauge that lies after the
        # task dies.
        resources(METRICS)
        return PlainTextResponse(METRICS.render(), media_type=PROMETHEUS)
