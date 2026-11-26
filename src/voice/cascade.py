"""The call, split into an ear, a brain and a mouth.

Step three of [docs/CASCADE.md](../../docs/CASCADE.md), and the one that can
fail. It is worth about Rs 1.27 a connected minute and it costs roughly 1.3
seconds on every turn a customer waits through, and that trade is the whole
reason this is behind a flag rather than shipped.

## Why it is cheaper

The Live API re-bills the entire context window on every generation, and on a
voice call that window is mostly audio at four times the text rate. The
caller's words are charged again, and again, for as long as they stay.

Here, nothing the caller says ever enters a model's context as audio. Whisper
turns it into text at $0.04 an audio hour, the text brain holds the history
itself, and the Live session is handed only the reply to read out - so it
accumulates its own voice and nothing else, and even that is cheap to drop
because a mouth carrying no conversation can be replaced at any moment.

## The three pieces, and what each already was

| | | |
|---|---|---|
| ear | `GroqWhisper` | built in step one, as a second opinion |
| brain | `ChatSession` | the chat tab, unchanged - same tools, same history trimming |
| mouth | `GeminiLiveSession` | the voice path, with its persona replaced by `SPEAK_ONLY` |

The brain being the chat session is the reason this is a small file. It
already holds history as text, already runs the tools, already trims on whole
exchanges so a function response never loses its call, and already meters
itself at flash-lite rates. Voice and chat now answer with the same code,
which is also the strongest argument for this shape: one agent, two surfaces.

## The mouth is a loudspeaker, not an agent

A Live session still holding the persona would *answer* the text it was
handed rather than read it. So its system instruction is replaced with one
that says only "speak this", it is given no tools, and the reply it is handed
has already been decided.

Probed against the live endpoint on 2026-11-15, four Tanglish lines through
one session, comparing what was sent against `outputTranscription`:

| line | | first audio |
|---|---|---|
| "Rendu nalla option irukku. Redmi 15C 5G, 22,499 - 50MP camera." | verbatim | 527 ms |
| "Poorvika-ku welcome! Sollunga, enna venum?" | verbatim | 406 ms |
| "Stock irukka-nu ippo check pannitten - 34 units, 24 branches la irukku." | verbatim | 494 ms |
| "Adhoda EMI month-ku 1,984 rupees, 48 months." | verbatim | 500 ms |

Word-for-word every time, numbers and all, and inside the 455-640 ms
PIPELINE.md measured for a speaker-only session. It did not answer the
question in line two, which is the failure this instruction exists to
prevent. If it ever does paraphrase, the trace shows it: the text we sent
and the transcript it produced are both recorded.

## What this does not change

`Conversation` cannot tell. This presents exactly the `VoiceModel` surface
the Live path does, the same way `RotatingLiveSession` does - the barge-in
gate, the VAD, the trace and the shadow ear all work unmodified. The flag
picks which model `Conversation` is handed and nothing above this file knows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from src.voice.events import (
    AudioOut,
    CallerTranscript,
    Event,
    Transcript,
    TurnComplete,
    TurnInterrupted,
)
from src.voice.usage import SessionUsage, TurnUsage
from src.voice.whisper import Heard

log = logging.getLogger(__name__)

# The mouth's whole prompt. Short on purpose: it is charged on every
# utterance, and there is nothing for it to reason about.
SPEAK_ONLY = (
    "You are a loudspeaker. Read the user's message aloud, word for word, "
    "exactly as written. Do not answer it, greet, add, remove, translate, "
    "summarise or comment. The message is already the finished reply - your "
    "only job is to say it. If it is empty, say nothing."
)

# A loudspeaker has nothing to be creative about. The agent runs at 0.7
# because it is choosing what to say; by the time the mouth sees the reply
# the choosing is over, and variation here can only be a price read
# differently from the one the tool returned.
MOUTH_TEMPERATURE = 0.0

# How often the mouth is sent a frame of silence while it has nothing to say.
#
# Found by running a whole conversation through this class: the recording
# socket came back 1008 "Session closed due to client inactivity" between
# utterances. On the Live path this cannot happen, because the microphone
# pump sends every frame - and when the gate is held shut it substitutes
# SILENCE rather than nothing, for exactly this reason. src/voice/session.py
# says so in a comment that was written after a trace showed 440 held frames
# and not one byte sent across eleven seconds.
#
# The cascade's mouth is the one Live socket in the system that is never sent
# audio at all. A customer who thinks for half a minute would have hung up on
# them. Phase 0 measured that silence is not billed.
KEEPALIVE_SECONDS = 5.0
KEEPALIVE_FRAME = bytes(640)

# Below this there is nothing to transcribe, only a gate opening and closing.
MIN_UTTERANCE_SECONDS = 0.4

# One utterance of a phone call. The buffer is capped so a transport that
# never reports a turn boundary cannot grow without limit.
MAX_UTTERANCE_SECONDS = 60.0


class Ear(Protocol):
    """The slice of `GroqWhisper` a cascade needs."""

    async def hear(self, pcm: bytes, *, rate: int) -> Heard: ...


class Brain(Protocol):
    """The slice of `ChatSession` a cascade needs."""

    def send(self, text: str) -> AsyncIterator[str]: ...

    def remember(self, context: str) -> None: ...

    @property
    def usage(self) -> SessionUsage: ...


class Loudspeaker(Protocol):
    """The slice of `GeminiLiveSession` a cascade needs to speak with."""

    async def say(self, text: str) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, *exc: Any) -> Any: ...


class CascadeSession:
    """Ear, brain and mouth, presented to `Conversation` as one model."""

    def __init__(
        self,
        ear: Ear,
        brain: Brain,
        mouth: Loudspeaker,
        *,
        rate: int,
        ear_timeout_s: float = 8.0,
        keepalive_s: float = KEEPALIVE_SECONDS,
        clock: Callable[[], float] = time.perf_counter,
        on_caller_speech: Callable[[str], None] | None = None,
    ) -> None:
        self._ear = ear
        self._brain = brain
        self._mouth = mouth
        self._rate = rate
        self._ear_timeout = ear_timeout_s
        self._keepalive_s = keepalive_s
        self._clock = clock
        # The prefetch listener. A cascade hears the caller all at once
        # rather than in fragments, so it fires later here than on the Live
        # path - after the transcript rather than during it. That is still
        # before the brain has decided anything, which is what it is for.
        self._on_caller_speech = on_caller_speech

        self._buffer = bytearray()
        self._cap = int(MAX_UTTERANCE_SECONDS * rate * 2)
        self._turn_id = 0
        self._out: asyncio.Queue[Event] = asyncio.Queue()
        self._speech_ended_at: float | None = None
        self._first_audio_at: float | None = None
        self._pump: asyncio.Task[None] | None = None
        self._thinking: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        # Set while the mouth has a turn in flight. The keepalive must not
        # send audio into a reply that is still being spoken: the Live API
        # reads incoming audio as the caller interrupting, and the turn ends
        # as TurnInterrupted instead of TurnComplete.
        self._mouth_busy = False
        # Utterances the ear could not read. Counted because the customer
        # hears nothing at all when this happens, which is indistinguishable
        # from the agent being slow.
        self._unheard = 0
        self.usage = SessionUsage()
        # How much of the brain's own running total has already been added
        # to `usage`, so each turn adds the difference and not the pile.
        self._brain_charged = 0.0
        # Never rotates: the mouth holds no conversation to carry across a
        # socket. Present so the call service can report it the same way.
        self.rotations = 0

    @property
    def turn_id(self) -> int:
        return self._turn_id

    async def __aenter__(self) -> CascadeSession:
        """Open the mouth. The ear and the brain are stateless HTTP."""
        await self._mouth.__aenter__()
        self._keepalive = asyncio.create_task(self._keep_open(), name="keepalive")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
        await self._mouth.__aexit__(*exc)

    # -- the VoiceModel surface -------------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        """Buffer a frame. Nothing is sent anywhere.

        This is the line the whole cost argument rests on: no caller audio
        reaches any model's context window, so none of it is re-billed on
        the turn after.

        DIGITAL SILENCE IS NOT CALLER AUDIO and is dropped. While the agent
        speaks, the microphone gate holds the caller's frames back and sends
        a run of zeroes in their place, to keep the Live socket from being
        closed as idle - `session.py` explains why. The Live model ignores
        it, having asked for it. The cascade was buffering it, so by the time
        the caller finished a sentence the ear was handed ten seconds of
        silence with two of speech inside it, and xAI answered 200 OK with an
        empty transcript, seven times in a row, on the first real call.

        A microphone never produces an exactly-zero frame; a substitution
        always does. That is the whole test.
        """
        if any(pcm) and len(self._buffer) < self._cap:
            self._buffer += pcm

    async def end_of_speech(self) -> None:
        """The caller stopped. Start the turn and return immediately.

        This is called from inside the microphone pump's own loop, so it may
        not await the turn: the Live path's version sends one JSON frame and
        comes straight back, and a version that blocked for the ear, the
        brain and the mouth - about 1.8 seconds - would stop reading the
        microphone for the whole of it and lose whatever the caller said
        next.
        """
        pcm, self._buffer = bytes(self._buffer), bytearray()
        self._turn_id += 1
        self._speech_ended_at = self._clock()
        self._first_audio_at = None
        if len(pcm) < MIN_UTTERANCE_SECONDS * self._rate * 2:
            # A gate that opened and closed. Answering it would be answering
            # nobody, and it would cost a Groq request to find that out.
            await self._out.put(
                TurnComplete(
                    turn_id=self._turn_id, usage=TurnUsage(), first_audio_ms=None
                )
            )
            return
        if self._thinking is not None and not self._thinking.done():
            # The caller spoke again before the last answer arrived, so that
            # answer is about a question they have moved on from.
            self._thinking.cancel()
        self._thinking = asyncio.create_task(
            self._turn(pcm, self._turn_id), name="cascade-turn"
        )

    def events(self) -> AsyncIterator[Event]:
        return self._events()

    async def _events(self) -> AsyncIterator[Event]:
        if self._pump is None:
            self._pump = asyncio.create_task(self._drain_mouth(), name="mouth")
        while True:
            yield await self._out.get()

    async def greet(self, text: str) -> None:
        """Speak first, without anyone having spoken."""
        self._turn_id += 1
        self._speech_ended_at = self._clock()
        self._first_audio_at = None
        await self._speak(text, self._turn_id)

    async def opened_with(self, text: str) -> None:
        """The greeting was played off disk; tell the brain it said it.

        The mouth is not involved - the recording has already gone to the
        caller through `SpeaksTheOpening`, and sending the line to the mouth
        as well would say it twice. What is missing is only that the brain,
        which holds this conversation's history, does not know the call has
        opened. See `src/voice/opening.py`.
        """
        if not text.strip():
            return
        self._brain.remember(text)

    async def prime(self, context: str) -> None:
        """Context for the brain, as a turn it is not asked to answer.

        The Live path sends this to the model with `turnComplete` false. Here
        it goes into the history the brain already holds, which is the same
        thing done by the side that owns the history.
        """
        if not context.strip():
            return
        self._brain.remember(context)

    async def _keep_open(self) -> None:
        """Send the mouth silence so the server does not close it on us.

        A stream that goes quiet is a stream that can be timed out, and this
        one is quiet by design between replies. Failures are ignored: if the
        socket has already gone, the next `say` will report it to the turn
        that needs it rather than here, where nobody is listening.
        """
        while True:
            await asyncio.sleep(self._keepalive_s)
            if self._mouth_busy:
                # It is speaking, so the socket is not idle and silence
                # would only interrupt it.
                continue
            try:
                await self._mouth.send_audio(KEEPALIVE_FRAME)
            except asyncio.CancelledError:
                raise
            except Exception as failed:  # noqa: BLE001
                log.info("mouth keepalive failed: %s", type(failed).__name__)

    async def close(self) -> None:
        for task in (self._pump, self._thinking, self._keepalive):
            if task is not None:
                task.cancel()

    # -- internals ---------------------------------------------------------

    async def _turn(self, pcm: bytes, turn_id: int) -> None:
        heard = await self._transcribe(pcm)
        if heard is None:
            self._unheard += 1
            # Nothing was understood. Saying so is the agent's job and it
            # cannot be done without words, so the turn ends silently and the
            # caller repeats themselves - which is what happens today when
            # the Live API mishears, and is not worse.
            await self._out.put(
                TurnComplete(turn_id=turn_id, usage=TurnUsage(), first_audio_ms=None)
            )
            return

        await self._out.put(CallerTranscript(turn_id=turn_id, text=heard))
        if self._on_caller_speech is not None:
            self._on_caller_speech(heard)

        reply = await self._think(heard)
        if not reply:
            await self._out.put(
                TurnComplete(
                    turn_id=turn_id, usage=self._turn_usage(), first_audio_ms=None
                )
            )
            return

        await self._out.put(Transcript(turn_id=turn_id, text=reply))
        await self._speak(reply, turn_id)

    async def _transcribe(self, pcm: bytes) -> str | None:
        try:
            heard = await asyncio.wait_for(
                self._ear.hear(pcm, rate=self._rate), self._ear_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001
            log.warning("cascade could not transcribe: %s", type(failed).__name__)
            return None
        return heard.text or None

    async def _think(self, heard: str) -> str:
        parts: list[str] = []
        try:
            async for delta in self._brain.send(heard):
                parts.append(delta)
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001
            log.warning("cascade brain failed: %s", type(failed).__name__)
            return ""
        return "".join(parts).strip()

    async def _speak(self, text: str, turn_id: int) -> None:
        self._mouth_busy = True
        try:
            await self._mouth.say(text)
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001
            # A mouth that will not speak ends the turn rather than hanging
            # it: `Conversation` waits for TurnComplete to release the latch
            # that lets the caller's next sentence be heard at all.
            log.warning("cascade mouth failed: %s", type(failed).__name__)
            self._mouth_busy = False
            await self._out.put(
                TurnComplete(
                    turn_id=turn_id, usage=self._turn_usage(), first_audio_ms=None
                )
            )

    async def _drain_mouth(self) -> None:
        """Forward the mouth's audio, retagged with our own turn ids.

        The mouth counts its own turns and they do not line up with ours - a
        turn where the brain answered without the mouth being asked would put
        them one apart for the rest of the call, and a consumer that drops
        audio for abandoned turns BY ID would then silence a live one.
        """
        try:
            async for event in self._mouth.events():
                match event:
                    case AudioOut(pcm=pcm):
                        if self._first_audio_at is None:
                            self._first_audio_at = self._clock()
                        await self._out.put(AudioOut(turn_id=self._turn_id, pcm=pcm))
                    case TurnComplete(usage=usage):
                        self._mouth_busy = False
                        await self._out.put(
                            TurnComplete(
                                turn_id=self._turn_id,
                                usage=self._turn_usage(usage),
                                first_audio_ms=self._first_audio_ms(),
                            )
                        )
                    case TurnInterrupted():
                        # The mouth's turn ended without completing. Measured
                        # on a real call: the greeting spoke and then nothing
                        # happened, because `case _` swallowed this and
                        # `Conversation` waits for a TurnComplete to release
                        # the microphone. 448 events, not one turn_complete,
                        # and a caller talking into a shut gate.
                        #
                        # An interruption of the mouth is not an interruption
                        # of the conversation - there is no caller on this
                        # socket to interrupt it - so it is reported as the
                        # turn ending, which is what it is.
                        log.info("mouth turn was interrupted; ending the turn")
                        self._mouth_busy = False
                        await self._out.put(
                            TurnComplete(
                                turn_id=self._turn_id,
                                usage=self._turn_usage(),
                                first_audio_ms=self._first_audio_ms(),
                            )
                        )
                    case _:
                        # The mouth has no tools, no caller and no persona,
                        # so its own transcript of what it was told to say
                        # adds nothing the Transcript we already sent does.
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001
            log.warning("cascade mouth stream ended: %s", type(failed).__name__)

    def _turn_usage(self, spoken: TurnUsage | None = None) -> TurnUsage:
        """The mouth's audio as a turn, plus whatever the brain spent since.

        Two models bill this turn on two different rate cards. The mouth's
        tokens belong in `turns`, where the Live rates apply to them. The
        brain prices itself on flash-lite's card inside its own
        `SessionUsage`, so what crosses here is dollars, as a delta - the
        whole point of the cascade is that this number is small, and it
        would be invisible if only the spoken half were reported.
        """
        usage = spoken or TurnUsage()
        self.usage.turns.append(usage)
        spent = self._brain.usage.total_usd
        self.usage.add_side(spent - self._brain_charged)
        self._brain_charged = spent
        return usage

    def _first_audio_ms(self) -> float | None:
        if self._speech_ended_at is None or self._first_audio_at is None:
            return None
        return (self._first_audio_at - self._speech_ended_at) * 1000
