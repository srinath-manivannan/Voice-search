"""In-memory audio transports, so session logic can be tested with no device.

These are not toys - the session tests depend on being able to assert exactly
what was written and exactly when it was discarded, which a real speaker cannot
tell you.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class FakeSource:
    """Yields the frames it was constructed with, then stops.

    `keep_open` models a microphone that is still live after those frames. A
    real one does not hang up mid-call, and `Conversation.run` cancels the
    other pump as soon as either side ends - so a source that stops early will
    cut the model off before it has finished speaking.
    """

    def __init__(
        self, frames: list[bytes], sample_rate: int = 16000, keep_open: bool = False
    ) -> None:
        self._frames = list(frames)
        self._sample_rate = sample_rate
        self._keep_open = keep_open
        self.closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame
            await asyncio.sleep(0)
        if self._keep_open:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class FakeSink:
    """Records writes, and records what was still unheard at each flush."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self._sample_rate = sample_rate
        self.written: list[bytes] = []
        self.flushes = 0
        self.discarded: list[bytes] = []
        self.closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    async def flush(self) -> None:
        self.flushes += 1
        self.discarded.extend(self.written)
        self.written = []

    async def close(self) -> None:
        self.closed = True
