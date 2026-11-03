"""Where audio comes from and where it goes.

This is the seam the whole Phase 1 -> Phase 2 move turns on. Right now audio
arrives from a laptop microphone and leaves through a speaker. In Phase 2 it
arrives from a phone call and leaves down the same call. Nothing above this
file should notice the difference, so the session logic is written against
these two protocols and never against a device.

`flush` is the one method that exists purely for barge-in. When a caller talks
over the agent, stopping the generator is not enough - audio already handed to
the output is still queued and will keep playing over them. `flush` is the
promise that queued audio can be thrown away, and Phase 0's target is that this
happens within about 300 ms of the interruption being detected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioSource(Protocol):
    """Produces caller audio as raw little-endian 16-bit mono PCM."""

    @property
    def sample_rate(self) -> int: ...

    def frames(self) -> AsyncIterator[bytes]:
        """Yield fixed-size PCM frames until closed."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    """Consumes agent audio as raw little-endian 16-bit mono PCM."""

    @property
    def sample_rate(self) -> int: ...

    async def write(self, pcm: bytes) -> None: ...

    async def flush(self) -> None:
        """Discard anything queued but not yet heard. Used for barge-in."""
        ...

    async def close(self) -> None: ...
