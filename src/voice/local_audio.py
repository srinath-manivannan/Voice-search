"""Microphone and speaker for Phase 1, via ALSA's command line tools.

Why subprocesses rather than a binding: `sounddevice` needs PortAudio, which is
a system package and an interactive sudo away on this machine. `arecord` and
`aplay` are already here, speak raw PCM on stdin/stdout, and add no dependency
at all.

This is deliberately throwaway. It exists so Phase 1 can hear and speak on a
laptop; Phase 2 replaces both classes with a telephony transport and nothing
above `src.voice.transport` changes. Keeping it behind that seam is what stops
the expedient choice from leaking into the design.

Barge-in is handled by killing the `aplay` process. The audio it has buffered
dies with it, which is both the fastest flush available and the reason this
shape was chosen over writing silence into a long-lived stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

FRAME_MS = 20


def _frame_bytes(sample_rate: int) -> int:
    # 16-bit mono: two bytes per sample.
    return sample_rate * FRAME_MS // 1000 * 2


class MicrophoneSource:
    """Caller audio from the default ALSA capture device."""

    def __init__(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._process: asyncio.subprocess.Process | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is None:
            self._process = await asyncio.create_subprocess_exec(
                "arecord",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(self._sample_rate),
                "-c",
                "1",
                "-t",
                "raw",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._process

    async def frames(self) -> AsyncIterator[bytes]:
        process = await self._start()
        assert process.stdout is not None
        size = _frame_bytes(self._sample_rate)
        while True:
            # readexactly, not read: a short frame would desynchronise the
            # sample stream and the model would hear clicks.
            try:
                yield await process.stdout.readexactly(size)
            except asyncio.IncompleteReadError:
                return

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.kill()
        await process.wait()


class SpeakerSink:
    """Agent audio to the default ALSA playback device."""

    def __init__(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._process: asyncio.subprocess.Process | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(self._sample_rate),
                "-c",
                "1",
                "-t",
                "raw",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._process

    async def write(self, pcm: bytes) -> None:
        process = await self._start()
        assert process.stdin is not None
        try:
            process.stdin.write(pcm)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # A flush killed the player between the check and the write. The
            # audio was meant to be discarded anyway, so this is not an error.
            self._process = None

    async def flush(self) -> None:
        """Stop immediately and discard buffered audio.

        Killing the player is what makes this fast. Closing stdin politely
        would let it finish playing everything already buffered, which is the
        opposite of what barge-in needs.
        """
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.kill()
        await process.wait()

    async def close(self) -> None:
        await self.flush()
