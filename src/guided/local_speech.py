"""Private speech worker. Model weights are provisioned before startup."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request


async def bounded_audio(request: Request, maximum: int = 960_000) -> bytes:
    """Up to 30 seconds of mono 16-bit PCM at 16 kHz."""
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise HTTPException(413, "Speech turn too long")
        data.extend(chunk)
    if len(data) < 640 or len(data) % 2:
        raise HTTPException(422, "Invalid PCM audio")
    return bytes(data)


def create_worker() -> FastAPI:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    import numpy as np

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.model = await asyncio.to_thread(
            WhisperModel,
            os.environ.get("GUIDED_WHISPER_MODEL", "small"),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        yield

    worker = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    slots = asyncio.Semaphore(1)

    @worker.post("/inference")
    async def inference(
        request: Request, lang: str = Query(pattern="^(ta|en|auto)$")
    ) -> dict[str, str]:
        pcm = await bounded_audio(request)

        def recognise() -> str:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            segments, _ = worker.state.model.transcribe(
                samples,
                language=None if lang == "auto" else lang,
                beam_size=3,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()[:300]

        if slots.locked():
            raise HTTPException(503, "Speech worker busy")
        async with slots:
            return {"text": await asyncio.to_thread(recognise)}

    return worker
