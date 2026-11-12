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

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from src.voice.events import AudioOut, Event, TurnComplete, Transcript, TurnInterrupted
from src.voice.trace import CallTrace
from src.voice.transport import AudioSink, AudioSource
from src.voice.vad import EnergyVad, VadFrame

log = logging.getLogger(__name__)


class VoiceModel(Protocol):
    """What this file needs from a model, and nothing more.

    Stated as a protocol because there are two: one socket for the whole
    call, and `RotatingLiveSession`, which is several and hides it. Naming
    the concrete class here would make the second one a special case of the
    first, when it is neither - it is the same conversation.
    """

    @property
    def turn_id(self) -> int: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def end_of_speech(self) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...


@dataclass
class TurnRecord:
    """What Phase 1 has to report: latency and cost, per turn."""

    turn_id: int
    first_audio_ms: float | None
    response_tokens: int
    thinking_tokens: int
    # The prompt the server charged this turn for. CUMULATIVE for the session,
    # which is how the Live API reports it and the whole reason cost is not
    # flat per minute: if every turn is charged its full accumulated prompt,
    # the first minute of a call is paid for again on every turn after it.
    # Without this in the trace, 86% of a call's cost was invisible - the only
    # figure we had was a total that said nothing about where it came from.
    prompt_audio_tokens: int = 0
    prompt_text_tokens: int = 0
    barge_in: bool = False


@dataclass
class ConversationResult:
    turns: list[TurnRecord] = field(default_factory=list)

    def latencies(self) -> list[float]:
        return [t.first_audio_ms for t in self.turns if t.first_audio_ms is not None]


class Conversation:
    def __init__(
        self,
        model: VoiceModel,
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
        # One turn in flight at a time. The same trace showed eleven
        # end_of_speech signals in forty-five seconds and not one completed
        # turn: every signal makes the model start a reply and abandon the one
        # before it, so a caller who pauses between phrases is served nothing
        # at all. After sixteen seconds it had stopped producing audio
        # entirely.
        self._awaiting_reply = False
        # The opening greeting is protected from interruption. A trace caught
        # it being cut at 3.2 seconds by a frame at rms 949 - loud enough to
        # clear the threshold, and impossible to tell from a louder speaker.
        # It is one short sentence, it is the first thing a customer hears,
        # and losing it reads as a broken line. Everything after it can be
        # interrupted normally. Reverse this if callers complain they cannot
        # cut the greeting off.
        self._in_greeting = agent_opens
        self._barge_frames = 0
        # Roughly 300 ms. While the microphone is held back these keep the run
        # up to a genuine interruption, so the caller's first syllable is not
        # lost when we start forwarding again.
        self._held: deque[bytes] = deque(maxlen=15)
        self._abandoned: set[int] = set()
        # Every decision this call makes, timestamped. Not the words - the
        # choices, which is what is missing when something goes wrong.
        self.trace = CallTrace()
        self.trace.record("call_open", agent_opens=agent_opens)
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
    # An absolute floor as well as a relative one. A trace showed a barge-in
    # firing at rms 103 against a noise floor of 1: in a quiet room the floor
    # decays to its minimum, at which point "six times the floor" means
    # "louder than six" and every echo clears it. Real speech into a laptop
    # microphone sits in the thousands; surviving echo sat at 103.
    BARGE_IN_MIN_RMS = 800.0

    # How much speech counts as the caller taking their turn. Eight 20 ms
    # frames is 160 ms - shorter than any syllable, long enough that the tail
    # of the agent's own sentence leaking through the speaker does not
    # qualify.
    SPEECH_FRAMES_FOR_A_TURN = 8

    def _past_grace(self) -> bool:
        return (
            time.monotonic() - self._agent_started_at
        ) >= self.BARGE_IN_GRACE_SECONDS

    def _is_real_interruption(self, verdict: VadFrame) -> bool:
        """Sustained and loud, or it is the agent hearing itself."""
        if self._in_greeting:
            return False
        loud = verdict.rms > max(
            self.BARGE_IN_MIN_RMS, verdict.noise_floor * self.BARGE_IN_RMS_FACTOR
        )
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
                    self.trace.record(
                        "gate_held", rms=int(verdict.rms), run=self._barge_frames
                    )
                    # Silence, not nothing. Withholding frames entirely leaves
                    # the model receiving no audio at all for the length of a
                    # reply - a trace showed 440 held frames and not one byte
                    # sent across eleven seconds - and a stream that goes quiet
                    # is a stream that can be timed out and closed. Silence
                    # keeps it alive, carries no echo for the model to mistake
                    # for the caller, and Phase 0 measured that it is not
                    # billed.
                    await self._model.send_audio(bytes(len(frame)))
                    continue

                log.debug(
                    "barge-in at rms=%.0f floor=%.0f", verdict.rms, verdict.noise_floor
                )
                self.trace.record(
                    "barge_in_local",
                    rms=int(verdict.rms),
                    floor=int(verdict.noise_floor),
                    held_frames=len(self._held),
                )
                await self._notify(type="barge_in")
                await self._interrupt()
                # Forward the run-up so the interruption does not arrive
                # beheaded, missing the word that started it.
                for held in self._held:
                    await self._model.send_audio(held)
                self._held.clear()
                self._barge_frames = 0

            # A run of speech, not one frame of it. TurnComplete means the
            # model stopped generating, not that the speaker stopped playing:
            # the browser still has audio queued, the gate opens, and the
            # microphone hears the agent's own tail. One such frame used to be
            # enough to count as "the caller spoke", and the next quiet frame
            # then fired end_of_speech four milliseconds after the agent
            # finished - a turn boundary for a turn nobody spoke in. It also
            # started the latency clock, so the next real reply was timed from
            # a moment the caller had not yet opened their mouth, which is
            # where a p95 of eight seconds came from.
            if verdict.speech_run >= self.SPEECH_FRAMES_FOR_A_TURN:
                self._heard_speech_this_turn = True

            if verdict.is_speech != self._caller_speaking:
                self._caller_speaking = verdict.is_speech
                await self._notify(type="state", caller_speaking=verdict.is_speech)

                if not verdict.is_speech and (
                    self._heard_speech_this_turn and not self._awaiting_reply
                ):
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
                    self._awaiting_reply = True
                    self.trace.record("end_of_speech", turn=self._model.turn_id + 1)
                    await self._model.end_of_speech()

            await self._model.send_audio(frame)

    async def _interrupt(self) -> None:
        """Stop talking over the caller, and remember not to resume."""
        self.trace.record("flush", turn=self._model.turn_id)
        # The reply we were waiting for is the one being abandoned.
        self._awaiting_reply = False
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
                        self.trace.record("audio_dropped", turn=turn_id, bytes=len(pcm))
                        continue
                    if not self._agent_speaking:
                        self._agent_speaking = True
                        self._agent_started_at = time.monotonic()
                        self.trace.record("agent_floor", turn=turn_id)
                        await self._notify(type="state", agent_speaking=True)
                    self.trace.record("audio_out", turn=turn_id, bytes=len(pcm))
                    await self._sink.write(pcm)

                case Transcript(text=text):
                    await self._notify(type="transcript", text=text)

                case TurnInterrupted(turn_id=turn_id):
                    # Distinct from our own barge-in: this is the model saying
                    # it heard the caller. Telling them apart mattered.
                    self.trace.record("interrupted_by_model", turn=turn_id)
                    self._abandoned.add(turn_id)
                    await self._interrupt()

                case TurnComplete(
                    turn_id=turn_id, usage=usage, first_audio_ms=first_audio_ms
                ):
                    self._agent_speaking = False
                    # The greeting is over; normal interruption resumes.
                    self._in_greeting = False
                    # Release the latch. Without this the first turn is the
                    # only turn: _awaiting_reply stays set for the rest of the
                    # call and the caller's later speech is never signalled.
                    # A trace showed one end_of_speech across forty seconds.
                    self._awaiting_reply = False
                    # Echo gathered while the agent spoke is not the caller.
                    self._held.clear()
                    self._barge_frames = 0
                    record = TurnRecord(
                        turn_id=turn_id,
                        first_audio_ms=first_audio_ms,
                        response_tokens=usage.response_audio + usage.response_text,
                        thinking_tokens=usage.thoughts,
                        prompt_audio_tokens=usage.prompt_audio,
                        prompt_text_tokens=usage.prompt_text,
                    )
                    self.result.turns.append(record)
                    self.trace.record(
                        "turn_complete",
                        turn=turn_id,
                        first_audio_ms=first_audio_ms,
                        response_tokens=record.response_tokens,
                        thinking_tokens=record.thinking_tokens,
                        prompt_audio=record.prompt_audio_tokens,
                        prompt_text=record.prompt_text_tokens,
                    )
                    await self._notify(
                        type="turn",
                        turn_id=record.turn_id,
                        first_audio_ms=record.first_audio_ms,
                        response_tokens=record.response_tokens,
                        thinking_tokens=record.thinking_tokens,
                        agent_speaking=False,
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
