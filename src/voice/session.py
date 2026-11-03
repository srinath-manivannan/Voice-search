"""Drives one conversation: microphone in, model in the middle, speaker out.

This is the composition point for Phase 1. It knows about the transport
protocols and the event vocabulary, and nothing about ALSA, websockets or
Gemini. Phase 2 hands it a telephony transport instead of a laptop and this
file does not change.

The barge-in rule is the part worth reading twice. Two things have to happen
when a caller talks over the agent, and they happen at different speeds:

  locally, now        our VAD sees speech while the agent is still talking, so
                      playback is flushed immediately - no round trip
  from the server     a TurnInterrupted arrives later confirming it, and any
                      audio still arriving for the abandoned turn is dropped
                      by turn id

Doing only the second is what makes an agent talk over people for a second
before stopping. Doing only the first would let stale audio resume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from collections.abc import Awaitable, Callable
from typing import Any

from src.voice.events import AudioOut, TurnComplete, Transcript, TurnInterrupted
from src.voice.gemini_live import GeminiLiveSession
from src.voice.transport import AudioSink, AudioSource
from src.voice.vad import EnergyVad, VadFrame

log = logging.getLogger(__name__)


@dataclass
class TurnRecord:
    """What Phase 1 has to report: latency and cost, per turn."""

    turn_id: int
    first_audio_ms: float | None
    response_tokens: int
    thinking_tokens: int
    barge_in: bool = False


@dataclass
class ConversationResult:
    turns: list[TurnRecord] = field(default_factory=list)

    def latencies(self) -> list[float]:
        return [t.first_audio_ms for t in self.turns if t.first_audio_ms is not None]


class Conversation:
    def __init__(
        self,
        model: GeminiLiveSession,
        source: AudioSource,
        sink: AudioSink,
        vad: EnergyVad | None = None,
        on_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        agent_opens: bool = False,
    ) -> None:
        self._model = model
        self._source = source
        self._sink = sink
        self._vad = vad or EnergyVad()
        # Optional, because the CLI does not need it and the telephony
        # transport will not have a screen to update.
        self._on_update = on_update
        # When the agent speaks first, it holds the floor from the moment the
        # greeting is requested - not from when its first audio chunk arrives
        # some 700 ms later. That gap was enough to lose the greeting: the
        # microphone was still open, a rise and fall in room noise read as the
        # caller finishing a sentence, end_of_speech told the model so, and the
        # model abandoned its greeting to answer. The floor has to be claimed
        # before the audio exists, not when it shows up.
        self._agent_speaking = agent_opens
        self._agent_started_at = time.monotonic() if agent_opens else 0.0
        self._caller_speaking = False
        self._heard_speech_this_turn = False
        self._barge_frames = 0
        # Roughly 300 ms. While the microphone is held back these keep the run
        # up to a genuine interruption, so the caller's first syllable is not
        # lost when we start forwarding again.
        self._held: deque[bytes] = deque(maxlen=15)
        self._abandoned: set[int] = set()
        self.result = ConversationResult()

    # The agent's own voice reaches the microphone. Browser echo cancellation
    # removes most of it and not all, and what is left is speech-shaped, which
    # is exactly what the VAD is built to notice. Without a guard the agent
    # hears itself, calls it a barge-in, and flushes its own reply - so every
    # answer is cut off a moment after it starts and the caller hears almost
    # nothing. The grace period is the fix: for this long after the agent
    # starts speaking, it does not listen for interruptions at all.
    BARGE_IN_GRACE_SECONDS = 0.4
    # A real interrupter speaks over the agent for a moment, and does it loudly
    # relative to the room. Echo surviving the browser's cancellation is neither
    # sustained nor loud, so demanding both is what separates them. 12 frames is
    # 240 ms.
    BARGE_IN_SPEECH_FRAMES = 12
    BARGE_IN_RMS_FACTOR = 6.0

    def _past_grace(self) -> bool:
        return (
            time.monotonic() - self._agent_started_at
        ) >= self.BARGE_IN_GRACE_SECONDS

    def _is_real_interruption(self, verdict: VadFrame) -> bool:
        """Sustained and loud, or it is the agent hearing itself."""
        loud = verdict.rms > verdict.noise_floor * self.BARGE_IN_RMS_FACTOR
        if verdict.is_speech and loud:
            self._barge_frames += 1
        else:
            self._barge_frames = 0
        return self._barge_frames >= self.BARGE_IN_SPEECH_FRAMES and self._past_grace()

    async def _notify(self, **payload: Any) -> None:
        if self._on_update is not None:
            await self._on_update(payload)

    async def _pump_microphone(self) -> None:
        """Caller audio to the model, with a local ear for barge-in.

        Audio is forwarded unconditionally. Phase 0 established the server does
        not bill silence, so gating here would buy nothing and would risk
        breaking the server's own turn detection.
        """
        async for frame in self._source.frames():
            verdict = self._vad.accept(frame)

            if self._agent_speaking:
                # Hold the microphone back rather than streaming the agent's
                # own voice into the model. Browser echo cancellation leaves
                # enough behind that the model hears itself, decides the caller
                # interrupted, and cuts its own sentence off - which is why the
                # greeting stopped partway through. Silence is not billed, so
                # holding costs nothing.
                if not self._is_real_interruption(verdict):
                    self._held.append(frame)
                    continue

                log.debug(
                    "barge-in at rms=%.0f floor=%.0f", verdict.rms, verdict.noise_floor
                )
                await self._notify(type="barge_in")
                await self._interrupt()
                # Forward the run-up so the interruption does not arrive
                # beheaded, missing the word that started it.
                for held in self._held:
                    await self._model.send_audio(held)
                self._held.clear()
                self._barge_frames = 0

            if verdict.is_speech != self._caller_speaking:
                self._caller_speaking = verdict.is_speech
                await self._notify(type="state", caller_speaking=verdict.is_speech)

                if verdict.is_speech:
                    self._heard_speech_this_turn = True
                elif self._heard_speech_this_turn:
                    # The caller has stopped. Say so.
                    #
                    # Leaving this out was the bug behind "sometimes it replies
                    # and sometimes it does not". end_of_speech is what advances
                    # the turn id, so without it every reply was tagged turn 0
                    # for the whole call - and the first interruption put turn 0
                    # in the abandoned set, after which every chunk the model
                    # sent was dropped on the way to the speaker. Silence, with
                    # a transcript still scrolling.
                    #
                    # It also starts the latency clock, so first-audio timing
                    # was never measured either.
                    self._heard_speech_this_turn = False
                    await self._model.end_of_speech()

            await self._model.send_audio(frame)

    async def _interrupt(self) -> None:
        """Stop talking over the caller, and remember not to resume."""
        self._agent_speaking = False
        self._abandoned.add(self._model.turn_id)
        await self._sink.flush()
        if self.result.turns:
            self.result.turns[-1].barge_in = True

    async def _pump_model(self) -> None:
        async for event in self._model.events():
            match event:
                case AudioOut(turn_id=turn_id, pcm=pcm):
                    # Audio for a turn the caller already talked over must not
                    # reach the speaker, however late it arrives.
                    if turn_id in self._abandoned:
                        continue
                    if not self._agent_speaking:
                        self._agent_speaking = True
                        self._agent_started_at = time.monotonic()
                        await self._notify(type="state", agent_speaking=True)
                    await self._sink.write(pcm)

                case Transcript(text=text):
                    await self._notify(type="transcript", text=text)

                case TurnInterrupted(turn_id=turn_id):
                    self._abandoned.add(turn_id)
                    await self._interrupt()

                case TurnComplete(
                    turn_id=turn_id, usage=usage, first_audio_ms=first_audio_ms
                ):
                    self._agent_speaking = False
                    self.result.turns.append(
                        TurnRecord(
                            turn_id=turn_id,
                            first_audio_ms=first_audio_ms,
                            response_tokens=usage.response_audio + usage.response_text,
                            thinking_tokens=usage.thoughts,
                        )
                    )

    async def run(self) -> ConversationResult:
        """Run until the microphone stops or the socket closes.

        Both pumps are cancelled when either finishes - a half-open
        conversation is worse than a closed one, because the caller hears
        nothing and no one knows why.
        """
        microphone = asyncio.create_task(self._pump_microphone(), name="microphone")
        model = asyncio.create_task(self._pump_model(), name="model")

        done, pending = await asyncio.wait(
            {microphone, model}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if (error := task.exception()) is not None:
                raise error

        return self.result
