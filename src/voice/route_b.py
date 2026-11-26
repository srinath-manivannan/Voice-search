"""Route B inside the cascade: deterministic lines, then a bounded model.

Route C answers from the words already in the utterance and does not call a
model. Route B hands the same text to the flash-lite session the cascade
already uses. Spoken lines are remembered by a hash of the text so an
identical sentence is replayed instead of synthesised again.

Native live audio stays on Route A, and only when this route is off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from src.guided.speech_cache import cache_key
from src.voice.events import AudioOut, TurnComplete, TurnInterrupted
from src.voice.router import choose
from src.voice.usage import TurnUsage

log = logging.getLogger(__name__)


def deterministic_reply(text: str) -> str:
    """A short English line. No model, no catalogue call, no retailer name."""
    folded = (text or "").strip()
    if not folded:
        return "Tell me what you are looking for."
    return (
        "Share a budget and the one thing that matters most, "
        "and I will shortlist products, offers and store availability."
    )


class RoutedBrain:
    """Pick Route C or the bounded text model for one utterance."""

    def __init__(self, inner: Any, *, route_b: bool, route_a: bool) -> None:
        self._inner = inner
        self._route_b = route_b
        self._route_a = route_a

    @property
    def usage(self) -> Any:
        return self._inner.usage

    def remember(self, context: str) -> None:
        self._inner.remember(context)

    async def send(self, text: str) -> AsyncIterator[str]:
        route = choose(text, route_b=self._route_b, route_a=self._route_a)
        if route == "C":
            yield deterministic_reply(text)
            return
        async for delta in self._inner.send(text):
            yield delta


class HashCachedMouth:
    """Replay a line whose text hash was spoken earlier in this call.

    The first time, audio is streamed from the inner mouth and stored.
    The next identical line is the stored PCM, with no new synthesis.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._pcm: dict[str, list[bytes]] = {}
        self._key = ""
        self._buf: list[bytes] = []
        self._out: asyncio.Queue[Any] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

    async def __aenter__(self) -> HashCachedMouth:
        await self._inner.__aenter__()
        self._pump = asyncio.create_task(self._forward(), name="route-b-mouth")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pump is not None:
            self._pump.cancel()
        await self._inner.__aexit__(*exc)

    async def say(self, text: str) -> None:
        key = cache_key("route-b", text)
        saved = self._pcm.get(key)
        if saved is not None:
            for pcm in saved:
                await self._out.put(AudioOut(turn_id=0, pcm=pcm))
            await self._out.put(
                TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=0.0)
            )
            return
        self._key = key
        self._buf = []
        await self._inner.say(text)

    async def send_audio(self, pcm: bytes) -> None:
        await self._inner.send_audio(pcm)

    async def events(self) -> AsyncIterator[Any]:
        while True:
            yield await self._out.get()

    async def _forward(self) -> None:
        try:
            async for event in self._inner.events():
                if self._key and isinstance(event, AudioOut) and event.pcm:
                    self._buf.append(event.pcm)
                if self._key and isinstance(event, (TurnComplete, TurnInterrupted)):
                    if self._buf:
                        self._pcm[self._key] = list(self._buf)
                    self._buf = []
                    self._key = ""
                await self._out.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001
            log.info("route B mouth stopped: %s", type(failed).__name__)
