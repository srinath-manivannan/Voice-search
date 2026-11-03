"""Audio to and from a browser, over one WebSocket.

This is the second implementation of the transport seam, and that is the point
of it. If `AudioSource` and `AudioSink` can carry a laptop microphone and a
browser without the session logic noticing, the odds of them carrying a phone
call in Phase 2 are much better. Building this now is cheaper than discovering
the seam is wrong when telephony is on the clock.

Audio moves as raw binary frames; everything else - state, transcript, numbers
for the screen - moves as JSON on the same socket. Keeping the two apart means
the hot path never parses anything.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Protocol


class WebSocketLike(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_json(self, data: Any) -> None: ...


class BrowserSource:
    """Caller audio arriving from the browser's microphone."""

    def __init__(self, sample_rate: int, max_queued_frames: int = 100) -> None:
        self._sample_rate = sample_rate
        # Bounded on purpose. If the model stalls, dropping the oldest audio is
        # better than growing a queue until the conversation is minutes behind
        # and the caller is talking to the past.
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=max_queued_frames
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def offer(self, pcm: bytes) -> None:
        """Called by the socket reader. Never blocks it."""
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(pcm)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        """Guarantee the end-of-stream sentinel lands.

        Suppressing QueueFull here looked harmless and was not: on a backed-up
        queue the sentinel was swallowed, `frames()` waited for audio that
        would never arrive, and the conversation never ended. Make room first.
        """
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()


class BrowserSink:
    """Agent audio sent down to the browser for playback."""

    def __init__(self, websocket: WebSocketLike, sample_rate: int) -> None:
        self._websocket = websocket
        self._sample_rate = sample_rate
        self._closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def write(self, pcm: bytes) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._websocket.send_bytes(pcm)

    async def flush(self) -> None:
        """Tell the browser to drop whatever it has queued.

        The audio we are abandoning is already across the network and sitting in
        the page's playback queue, so the only thing that can discard it is the
        page. This message is that instruction, and the page stops every
        scheduled buffer when it arrives.
        """
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._websocket.send_json({"type": "flush"})

    async def close(self) -> None:
        self._closed = True
