"""A mouth that is not Gemini.

The cascade's mouth was a second Gemini Live socket: text in, audio out, no
tools and no persona. It worked, and it carried everything a Live session
carries - a keepalive so the server does not close it for inactivity, a
`1008 client inactivity` when that lapsed, a second billed Gemini session,
and audio output at $12 per million tokens.

xAI's TTS is a websocket that takes text and returns PCM, and nothing else.
Measured against the Live mouth on the call of 2026-11-16:

    Grok TTS, warm connection    390-455 ms to first audio
    agent speech, 25s of it      Rs 0.55   against Rs 0.79 on Live

**It is billed by the CHARACTER, not the token**, which is why its usage
does not go through `TurnUsage` - see `cost.grok_tts_inr`. A line that
reports characters as tokens would be wrong by about four times, in the one
panel whose job is to be checkable against a bill.

**Tamil is not in the documented language list.** xAI lists twenty - en, ar,
bn, zh, fr, de, hi, id, it, ja, ko, pt, ru, es, tr, vi - and says "additional
languages may work with varying accuracy". `ta` is accepted and returns
audio; whether that audio is good Tamil is a judgement nothing here can
make. That is the one thing to listen for before this goes anywhere near a
customer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import websockets

from src.voice.events import AudioOut, Event, TurnComplete
from src.voice.usage import TurnUsage

log = logging.getLogger(__name__)

HOST = "wss://api.x.ai/v1/tts"

# 2 is the lowest time-to-first-audio the docs offer. The REST reference
# lists only 0 and 1 and the TTS page lists 0, 1 and 2; 2 was sent in the
# latency probe and the server accepted it.
LOWEST_LATENCY = 2

# How long to wait for the audio of one reply. A mouth that never answers
# must not hold the turn open: `Conversation` releases the microphone on a
# TurnComplete, and a caller talking into a shut gate is the worst outcome
# this file has.
SPEAK_TIMEOUT_SECONDS = 20.0


class GrokMouthError(RuntimeError):
    """The mouth could not speak. The cascade ends the turn on this."""


class GrokMouth:
    """Text in, PCM out, over one websocket held for the call.

    The surface is the half of `GeminiLiveSession` the cascade uses as a
    mouth - `say`, `events`, the context manager and a `send_audio` that
    does nothing - so `CascadeSession` cannot tell which one it was given.
    """

    def __init__(
        self,
        api_key: str,
        *,
        rate: int = 24000,
        voice: str = "eve",
        language: str = "ta",
        connect: Any = None,
    ) -> None:
        self._api_key = api_key
        self._rate = rate
        self._voice = voice
        self._language = language
        self._connect = connect or websockets.connect
        self._socket: Any = None
        self._out: asyncio.Queue[Event] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        # Characters asked for, over the whole call. The bill is per
        # character and this is the only number that produces it.
        self.characters = 0

    @property
    def url(self) -> str:
        return (
            HOST
            + "?"
            + urlencode(
                {
                    "language": self._language,
                    "voice": self._voice,
                    "codec": "pcm",
                    "sample_rate": self._rate,
                    "optimize_streaming_latency": LOWEST_LATENCY,
                }
            )
        )

    async def __aenter__(self) -> GrokMouth:
        if not self._api_key:
            raise GrokMouthError("XAI_API_KEY is empty")
        # Opened once and held. The handshake is about 600 ms for a distant
        # client by xAI's own docs, and 944 ms measured from here - which is
        # why it happens at call setup, behind the recorded greeting, and
        # never in front of a reply.
        self._socket = await self._connect(
            self.url, additional_headers={"Authorization": f"Bearer {self._api_key}"}
        )
        self._reader = asyncio.create_task(self._read(), name="grok-mouth")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._socket is not None:
            await self._socket.close()
            self._socket = None

    async def send_audio(self, pcm: bytes) -> None:
        """Nothing. A TTS socket has no ear and needs no keepalive.

        The cascade sends its mouth a silent frame every few seconds because
        a Live session is closed for inactivity. This one is not, and
        dropping the frame here is cheaper than teaching the cascade which
        mouth it is holding.
        """
        return None

    async def say(self, text: str) -> None:
        """Speak this. Returns as soon as it is sent, not when it is said."""
        if self._socket is None:
            raise GrokMouthError("mouth is not connected")
        spoken = text.strip()
        if not spoken:
            # Nothing to say, but the turn still has to end or the gate
            # stays shut. Report it as a completed turn with no audio.
            await self._out.put(
                TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=None)
            )
            return
        self.characters += len(spoken)
        await self._socket.send(json.dumps({"type": "text.delta", "delta": spoken}))
        await self._socket.send(json.dumps({"type": "text.done"}))

    def events(self) -> AsyncIterator[Event]:
        return self._events()

    async def _events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._out.get()

    async def _read(self) -> None:
        """Audio frames to the queue, until the socket closes."""
        assert self._socket is not None  # noqa: S101 - set in __aenter__
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "audio.delta":
                    pcm = base64.b64decode(message.get("delta") or "")
                    if pcm:
                        await self._out.put(AudioOut(turn_id=0, pcm=pcm))
                elif kind == "audio.done":
                    # Billed by the character, so the token usage is empty
                    # on purpose. `self.characters` is what the meter reads.
                    await self._out.put(
                        TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=None)
                    )
                elif kind == "error":
                    # Ending the turn rather than raising: the caller is
                    # waiting with the microphone shut, and a turn that never
                    # completes never opens it again.
                    log.warning("grok tts error: %s", str(message)[:200])
                    await self._out.put(
                        TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=None)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001 - the call must not hear this
            log.warning("grok mouth stream ended: %s", type(failed).__name__)
            await self._out.put(
                TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=None)
            )
