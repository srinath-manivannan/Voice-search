"""Gemini Live adapter - the one file that knows the provider's wire protocol.

Everything above this speaks `src.voice.events`. Swapping provider means a new
file here and one line in the composition root, which is the seam the
architecture decision in PHASE0-REPORT.md depends on.

Two Phase 0 measurements are wired in as defaults rather than left to callers:

  thinkingBudget = 0     thinking is billed and is absent from totalTokenCount.
                         Measured at 126 tokens/turn, net about Rs 0.14/min.
  reply word cap         "under 15 words" instead of "under 25" roughly halved
                         response tokens, and output audio is the largest single
                         cost line in the whole stack.

The websocket factory is injected so the whole adapter can be tested against a
scripted socket without spending money. Tests that do spend are marked `live`.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from src.config import Config
from src.voice.events import AudioOut, Event, Transcript, TurnComplete, TurnInterrupted
from src.voice.usage import SessionUsage, TurnUsage

HOST = "generativelanguage.googleapis.com"
PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


class Socket(Protocol):
    """The slice of a websocket this adapter uses. Narrow on purpose."""

    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connect = Callable[[str], Awaitable[Socket]]


class _ClosedSocket(Exception):
    """The peer closed the stream. Normal at the end of a call."""


class _WebsocketAdapter:
    """Translates the library's close exceptions into one the adapter knows.

    Without this, a perfectly ordinary hang-up surfaces as a
    `websockets.exceptions.ConnectionClosedOK` traceback in the caller's face,
    and this module would have to import the library at the top just to name
    the exception it wants to ignore.
    """

    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, data: str) -> None:
        import websockets.exceptions as wse

        try:
            await self._socket.send(data)
        except wse.ConnectionClosed as closed:
            raise _ClosedSocket from closed

    async def recv(self) -> str | bytes:
        import websockets.exceptions as wse

        try:
            return await self._socket.recv()
        except wse.ConnectionClosed as closed:
            raise _ClosedSocket from closed

    async def close(self) -> None:
        await self._socket.close()


async def _default_connect(url: str) -> Socket:
    import websockets

    return _WebsocketAdapter(await websockets.connect(url, max_size=None))


class SetupFailed(RuntimeError):
    """The server rejected the session before any audio moved.

    Raised rather than swallowed: a bad model id or an unsupported config should
    stop the call immediately, not surface later as silence on the line.
    """


class GeminiLiveSession:
    """One Live API conversation.

    Usage is duplex - drive `send_audio` / `end_of_speech` from one task and
    iterate `events()` from another.
    """

    def __init__(
        self,
        config: Config,
        connect: Connect | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._config = config
        self._connect = connect or _default_connect
        self._clock = clock
        self._socket: Socket | None = None
        self._turn_id = 0
        self._speech_ended_at: float | None = None
        self._first_audio_at: float | None = None
        self._pending_usage = TurnUsage()
        self.usage = SessionUsage()

    @property
    def turn_id(self) -> int:
        """The turn currently being spoken. Used to discard abandoned audio."""
        return self._turn_id

    def _setup_message(self) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "responseModalities": ["AUDIO"],
            "temperature": 0.7,
        }
        # A budget of 0 switches thinking off entirely; measured, not assumed.
        # Omitting the key leaves the model's default on, which costs money and
        # latency, so this is set explicitly even when the value is the default.
        generation["thinkingConfig"] = {"thinkingBudget": self._config.thinking_budget}

        return {
            "setup": {
                "model": f"models/{self._config.gemini_live_model}",
                "generationConfig": generation,
                "systemInstruction": {
                    "parts": [{"text": self._config.system_instruction}]
                },
                "outputAudioTranscription": {},
            }
        }

    async def __aenter__(self) -> GeminiLiveSession:
        url = f"wss://{HOST}{PATH}?key={self._config.gemini_api_key}"
        self._socket = await self._connect(url)
        await self._socket.send(json.dumps(self._setup_message()))

        while True:
            message = self._decode(await self._socket.recv())
            if "setupComplete" in message:
                return self
            if "error" in message:
                # Never include the raw message verbatim - the URL carries the
                # API key as a query parameter and servers echo it back.
                raise SetupFailed(f"Live API rejected setup: {message.get('error')}")

    async def __aexit__(self, *_exc: object) -> None:
        socket = self._socket
        self._socket = None
        close = getattr(socket, "close", None)
        if close is not None:
            await close()

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, Any]:
        return json.loads(raw if isinstance(raw, str) else raw.decode())

    def _require_socket(self) -> Socket:
        if self._socket is None:
            raise RuntimeError("session is not connected - use `async with`")
        return self._socket

    async def send_audio(self, pcm: bytes) -> None:
        """Stream one frame of caller audio. Silent no-op once the peer hangs up.

        Phase 0 measured that the server discards silence before tokenising, so
        there is no cost reason to gate this client-side. Gate it for latency or
        turn-detection reasons if at all.
        """
        try:
            await self._require_socket().send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "audio": {
                                "mimeType": f"audio/pcm;rate={self._config.capture_sample_rate}",
                                "data": base64.b64encode(pcm).decode("ascii"),
                            }
                        }
                    }
                )
            )
        except _ClosedSocket:
            return

    async def greet(self, text: str) -> None:
        """Have the agent speak first, saying exactly this.

        Without it a caller hears nothing until they speak, which on a phone
        line reads as a dead call - the one thing an answered call must never
        sound like. The chat tab already opened with this sentence; voice
        waited in silence.

        The wording is pinned rather than left to the model, because a greeting
        that varies run to run is not a greeting, it is a sample. This counts
        as a turn, so its audio carries an id of its own and a later
        interruption cannot retroactively discard it.
        """
        self._turn_id += 1
        self._speech_ended_at = self._clock()
        self._first_audio_at = None
        await self._require_socket().send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": "Greet the customer now. Say this "
                                        "aloud exactly as written, every word of "
                                        "it, to the end. Do not shorten it, do "
                                        "not translate it, do not change its "
                                        "script, and ignore your reply length "
                                        f"limit for this one message: {text}"
                                    }
                                ],
                            }
                        ],
                        "turnComplete": True,
                    }
                }
            )
        )

    async def end_of_speech(self) -> None:
        """Tell the server the caller stopped talking, and start the latency clock.

        Signalling explicitly rather than waiting for the server's own silence
        timeout is what makes the measured latency comparable to what a caller
        perceives: our VAD already knows, so waiting would add dead air.
        """
        self._turn_id += 1
        self._speech_ended_at = self._clock()
        self._first_audio_at = None
        await self._require_socket().send(
            json.dumps({"realtimeInput": {"audioStreamEnd": True}})
        )

    async def events(self) -> AsyncIterator[Event]:
        """Yield model output until the socket closes."""
        socket = self._require_socket()
        while True:
            try:
                raw = await socket.recv()
            except (StopAsyncIteration, ConnectionError, _ClosedSocket):
                return
            if raw is None:
                return

            message = self._decode(raw)
            turn_id = self._turn_id

            if usage_metadata := message.get("usageMetadata"):
                self._pending_usage = self.usage.record(usage_metadata)

            server_content = message.get("serverContent") or {}

            if server_content.get("interrupted"):
                yield TurnInterrupted(turn_id=turn_id)
                continue

            for part in (server_content.get("modelTurn") or {}).get("parts") or []:
                inline = part.get("inlineData") or {}
                if data := inline.get("data"):
                    if self._first_audio_at is None:
                        self._first_audio_at = self._clock()
                    yield AudioOut(turn_id=turn_id, pcm=base64.b64decode(data))

            if (
                transcription := server_content.get("outputTranscription")
            ) and transcription.get("text"):
                yield Transcript(turn_id=turn_id, text=transcription["text"])

            if server_content.get("turnComplete"):
                yield TurnComplete(
                    turn_id=turn_id,
                    usage=self._pending_usage,
                    first_audio_ms=self._first_audio_ms(),
                )

    def _first_audio_ms(self) -> float | None:
        if self._speech_ended_at is None or self._first_audio_at is None:
            return None
        return (self._first_audio_at - self._speech_ended_at) * 1000
