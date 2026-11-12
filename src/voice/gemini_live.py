"""Gemini Live adapter - the one file that knows the provider's wire protocol.

Everything above this speaks `src.voice.events`. Swapping provider means a new
file here and one line in the composition root, which is the seam the
architecture decision in ../PHASE0-REPORT.md depends on.

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
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from src.config import Config
from src.voice.events import AudioOut, Event, Transcript, TurnComplete, TurnInterrupted
from src.voice.usage import SessionUsage, TurnUsage

log = logging.getLogger(__name__)

HOST = "generativelanguage.googleapis.com"
PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


class Socket(Protocol):
    """The slice of a websocket this adapter uses. Narrow on purpose."""

    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connect = Callable[[str], Awaitable[Socket]]


class _ClosedSocket(Exception):
    """The peer closed the stream.

    Normal at the end of a call - and NOT normal in the middle of one, which
    is why the close frame's code and reason are carried here rather than
    discarded. Google states the reason for a refusal in that frame and
    nowhere else: no error message arrives on the stream first. Swallowing it
    turned "the API rejected what we sent" into a call that simply stopped
    with nothing in the log, which is the worst possible way to find out.
    """

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(f"{code} {reason}".strip() or "closed")
        self.code = code
        self.reason = reason

    @property
    def expected(self) -> bool:
        """A hang-up, as opposed to the far end objecting to something."""
        return self.code in (None, 1000, 1001, 1005)


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
            raise _ClosedSocket(closed.code, closed.reason or "") from closed

    async def recv(self) -> str | bytes:
        import websockets.exceptions as wse

        try:
            # The library is untyped at this seam; the protocol above says
            # what we accept, and _decode rejects anything else.
            received: str | bytes = await self._socket.recv()
            return received
        except wse.ConnectionClosed as closed:
            raise _ClosedSocket(closed.code, closed.reason or "") from closed

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
        tools: dict[str, Any] | None = None,
        on_tool_call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self._config = config
        # Declared to the model at setup; executed by the injected callback.
        # The adapter stays ignorant of what the tools actually do, which is
        # what lets the product logic outlive a change of provider.
        self._tools = tools
        self._on_tool_call = on_tool_call
        self._connect = connect or _default_connect
        self._clock = clock
        self._socket: Socket | None = None
        self._turn_id = 0
        self._speech_ended_at: float | None = None
        self._first_audio_at: float | None = None
        self._pending_usage = TurnUsage()
        # Set at connect time: did the server let us switch thinking off?
        self.thinking_disabled = False
        self.usage = SessionUsage()

    @property
    def turn_id(self) -> int:
        """The turn currently being spoken. Used to discard abandoned audio."""
        return self._turn_id

    def _setup_message(self, with_thinking_config: bool = True) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "responseModalities": ["AUDIO"],
            "temperature": 0.7,
        }
        # A budget of 0 switches thinking off entirely; measured, not assumed.
        # Omitting the key leaves the model's default on, which costs money and
        # latency, so this is set explicitly even when the value is the default.
        if with_thinking_config:
            generation["thinkingConfig"] = {
                "thinkingBudget": self._config.thinking_budget
            }

        setup: dict[str, Any] = {
            "model": f"models/{self._config.gemini_live_model}",
            "generationConfig": generation,
            "systemInstruction": {"parts": [{"text": self._config.system_instruction}]},
            "outputAudioTranscription": {},
        }
        # Keep the session's context from growing for the whole call.
        #
        # Audio is billed at 25 tokens a second, and the prompt the server
        # charges for is CUMULATIVE: a three-minute call ends up carrying
        # every second of itself. Whether that whole window is charged again
        # on every turn is the open question, and the answer decides whether
        # a long call costs Rs 1.36 a minute or Rs 7. Rather than wait for
        # the answer, keep the window small enough that it does not matter.
        #
        # A sliding window drops the oldest exchanges and keeps the recent
        # ones, which is what a phone conversation can afford to lose. The
        # shortlist a caller refers back to - "second one", "antha Vivo" -
        # does not live in this window at all: it is held in ToolSession on
        # our side, so compression cannot take it away.
        if self._config.context_trigger_tokens:
            setup["contextWindowCompression"] = {
                "triggerTokens": self._config.context_trigger_tokens,
                "slidingWindow": {"targetTokens": self._config.context_target_tokens},
            }
        if self._tools:
            setup["tools"] = [self._tools]
        return {"setup": setup}

    async def __aenter__(self) -> GeminiLiveSession:
        """Connect, preferring to switch thinking off, but not insisting.

        thinkingConfig was accepted on this model and measured to work - 126
        thinking tokens a turn down to zero - and then started being refused
        with "Thinking budget is not supported for this model", closing the
        socket at setup. The capability came and went without notice, which is
        what a preview model is entitled to do.

        So it is attempted and not depended on. If the server refuses it, the
        session reconnects without it rather than failing the call. Thinking
        then costs what it costs - roughly Rs 0.14 a minute by the Phase 0
        measurement - and `thinking_disabled` says which happened.
        """
        self.thinking_disabled = await self._open(with_thinking_config=True)
        if not self.thinking_disabled:
            log.warning(
                "model refused thinkingConfig; reconnected without it. "
                "Thinking tokens are billed and absent from totalTokenCount."
            )
        return self

    async def _open(self, with_thinking_config: bool) -> bool:
        """Returns True when the session was opened with thinking switched off."""
        url = f"wss://{HOST}{PATH}?key={self._config.gemini_api_key}"
        self._socket = await self._connect(url)
        try:
            await self._socket.send(
                json.dumps(self._setup_message(with_thinking_config))
            )
            return await self._await_setup(with_thinking_config)
        except _ClosedSocket:
            # The server closes the socket on a setup it will not accept, so
            # the refusal arrives as a close rather than an error message.
            if not with_thinking_config:
                raise SetupFailed(
                    "Live API closed the connection during setup"
                ) from None
            await self._open(with_thinking_config=False)
            return False

    async def _await_setup(self, with_thinking_config: bool) -> bool:
        while True:
            message = self._decode(await self._socket.recv())  # type: ignore[union-attr]
            if "setupComplete" in message:
                return with_thinking_config
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
        decoded: dict[str, Any] = json.loads(
            raw if isinstance(raw, str) else raw.decode()
        )
        return decoded

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
            except _ClosedSocket as closed:
                # A mid-call close is the API objecting to something we sent,
                # and the close frame is the only place it says what. Losing
                # that reason costs an afternoon every time.
                if not closed.expected:
                    log.error(
                        "Live API closed the call: %s %s",
                        closed.code,
                        closed.reason or "(no reason given)",
                    )
                return
            except (StopAsyncIteration, ConnectionError):
                return
            if raw is None:
                return

            message = self._decode(raw)
            turn_id = self._turn_id

            if usage_metadata := message.get("usageMetadata"):
                self._pending_usage = self.usage.record(usage_metadata)

            # The model asking for a tool. Run it and answer on the same
            # socket; the model then speaks with the result in hand.
            if tool_call := message.get("toolCall"):
                await self._answer_tool_call(tool_call)
                continue

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

    async def _answer_tool_call(self, tool_call: dict[str, Any]) -> None:
        """Execute what the model asked for and send the result straight back.

        A failure is reported as a failure, not as an empty result: the model
        must be able to tell "nothing matched" from "the lookup did not
        happen", because those are different things to say to a customer.
        """
        responses = []
        for call in tool_call.get("functionCalls") or []:
            name = call.get("name", "")
            if self._on_tool_call is None:
                result: dict[str, Any] = {"error": "no_tool_handler"}
            else:
                try:
                    result = await self._on_tool_call(name, call.get("args") or {})
                except Exception:  # noqa: BLE001 - the call must still be answered
                    log.exception("tool %s failed", name)
                    result = {"error": "tool_failed"}
            responses.append({"id": call.get("id"), "name": name, "response": result})

        if responses:
            try:
                await self._require_socket().send(
                    json.dumps({"toolResponse": {"functionResponses": responses}})
                )
            except _ClosedSocket as closed:
                # The caller hung up while the tool was running - ordinary.
                # Anything else means the API dropped us between asking for
                # the tool and being told the answer, and then the reason in
                # that frame is the whole diagnosis.
                if not closed.expected:
                    log.error(
                        "Live API closed while we answered %s: %s %s",
                        ", ".join(r["name"] for r in responses),
                        closed.code,
                        closed.reason or "(no reason given)",
                    )

    def _first_audio_ms(self) -> float | None:
        if self._speech_ended_at is None or self._first_audio_at is None:
            return None
        return (self._first_audio_at - self._speech_ended_at) * 1000
