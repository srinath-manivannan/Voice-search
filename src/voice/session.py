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
from dataclasses import dataclass, field

from src.voice.events import AudioOut, TurnComplete, TurnInterrupted
from src.voice.gemini_live import GeminiLiveSession
from src.voice.transport import AudioSink, AudioSource
from src.voice.vad import EnergyVad

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
    ) -> None:
        self._model = model
        self._source = source
        self._sink = sink
        self._vad = vad or EnergyVad()
        self._agent_speaking = False
        self._abandoned: set[int] = set()
        self.result = ConversationResult()

    async def _pump_microphone(self) -> None:
        """Caller audio to the model, with a local ear for barge-in.

        Audio is forwarded unconditionally. Phase 0 established the server does
        not bill silence, so gating here would buy nothing and would risk
        breaking the server's own turn detection.
        """
        async for frame in self._source.frames():
            verdict = self._vad.accept(frame)

            if verdict.is_speech and self._agent_speaking:
                log.debug(
                    "barge-in at rms=%.0f floor=%.0f", verdict.rms, verdict.noise_floor
                )
                await self._interrupt()

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
                    self._agent_speaking = True
                    await self._sink.write(pcm)

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
