"""The opening line, played rather than generated.

The greeting is fixed text. persona.py pins the wording, and the instruction
that used to carry it told the model to say it "exactly as written, in full,
do not shorten it, do not translate it". A sentence that is not allowed to
vary does not need a model to produce it - but every call was buying one
anyway, and paying the full standing prompt for the privilege.

On the traced call of 2026-11-15 that was turn 1: Rs 0.45 of a Rs 5.46 bill,
1,561 prompt tokens, 177 response tokens, 68 thinking tokens, and 485 ms of
silence before the customer heard a word. Replacing it with a recording takes
all of that to zero - it is the only lever in this service that makes the
call cheaper AND faster, because every other saving trades one for the other.

**How it stays honest.** `SpeaksTheOpening` wraps a `VoiceModel` and puts the
recording into the event stream as `AudioOut` chunks followed by a
`TurnComplete`, which is exactly the shape a generated greeting had. Nothing
downstream can tell the difference, so the floor, the microphone gate, the
barge-in protection and the closing drain all keep working as they were
written and tested - this adds no new path through `session.py`, which is the
file in this repo with the worst history of subtle, call-killing bugs.

**The model is still told.** It did not produce the line, so it does not know
it said it. `opened_with()` puts the greeting into the session as a model
turn with `turnComplete` false: context, not a request, and not a generation.
About thirty tokens, re-billed on later turns like any other context, against
the Rs 0.45 it replaces.

**Staleness.** The recording has to be in the same voice as everything said
after it, or the customer hears the speaker change mid-conversation. The
recorder builds its session exactly as `voice_call.py` does, so the two
cannot drift through configuration - but they CAN drift if Google changes
the default voice for the model, and nothing here can detect that. If the
greeting ever sounds unlike the rest of the call, run `make greeting` again.
Deleting the file is always safe: the call falls back to generating it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from src.voice.events import AudioOut, Event, TurnComplete
from src.voice.session import VoiceModel
from src.voice.usage import TurnUsage

log = logging.getLogger(__name__)

# Beside persona.py, which holds the words it is a recording of. The two
# belong together: change the text and this file is wrong until it is
# re-recorded, and a reader who finds one should trip over the other.
RECORDING = Path(__file__).resolve().parent.parent / "agent" / "greeting.pcm"

# The line played while a tool round trip is in flight. Same machinery, same
# voice, same reason it is a recording: it is fixed text, so paying a model
# to produce it would be paying for silence to be filled.
HOLDING_RECORDING = Path(__file__).resolve().parent.parent / "agent" / "holding.pcm"

# Signed 16-bit mono, which is what the Live API returns and what the browser
# is set up to play. Stored raw rather than as a WAV because there is exactly
# one consumer and it already knows the rate from the config - a header would
# be a second place for the rate to be stated, and so a second place for it
# to be wrong.
BYTES_PER_SAMPLE = 2

# The turn the recording is attributed to. Zero is free: `_turn_id` on a live
# session starts there and is incremented BEFORE a turn's events are emitted,
# so the model's own first turn is 1. Abandoned-audio tracking compares turn
# ids, so a collision here would let a later interruption discard the
# greeting retroactively.
OPENING_TURN = 0

# How much audio goes out in one event. The live model emits roughly this -
# 190 chunks across about thirty seconds of speech on the traced call - and
# matching it keeps the browser's queue behaving the way it was tuned for.
CHUNK_SECONDS = 0.2

# Streamed at REAL TIME, and this is the whole of what went wrong the first
# time. It used to be 0.25 - four times real time - on the stated reasoning
# that "the model finishes generating long before the browser finishes
# playing". That reasoning was wrong, and the traces say so:
#
#   generated greeting   first chunk 1512 ms, turn_complete 8380 ms
#   recorded greeting    first chunk  907 ms, turn_complete 2780 ms
#
# Both carry about seven seconds of audio. The model's TurnComplete lands
# AFTER its audio is done; mine landed 5.1 seconds before. And TurnComplete
# is what gives the floor back - it opens the microphone and clears the
# greeting's barge-in protection. So the gate opened while the browser was
# still playing the greeting out loud, the microphone heard the agent's own
# voice, end_of_speech fired at 4.5 s on that echo, and the model was handed
# a turn made of its own greeting. It never replied: one turn, zero tokens,
# Rs 0.00 on screen for a call that had said nothing.
#
# Emitting audio faster than it plays is free for a sink that queues. It is
# not free for anything that reads TurnComplete as "the agent has stopped
# talking", which in this service is everything.
PACE = 1.0


def bytes_to_seconds(pcm: bytes, rate: int) -> float:
    """How long this many bytes will take to play."""
    if rate <= 0:
        return 0.0
    return len(pcm) / (rate * BYTES_PER_SAMPLE)


def load_recording(path: Path = RECORDING) -> bytes | None:
    """The recorded greeting, or None if there is not one.

    None is a supported state, not a failure: a fresh clone has no recording
    until `make greeting` is run, and the call simply generates its greeting
    the way it always did. Returning None rather than raising is what makes
    deleting the file a safe way to undo this.
    """
    try:
        pcm = path.read_bytes()
    except OSError:
        return None
    if not pcm:
        log.warning("%s is empty; the greeting will be generated instead", path)
        return None
    return pcm


class SpeaksTheOpening:
    """A `VoiceModel` that plays a recorded greeting before the real one.

    Everything except `events` is handed straight through. The recording is
    emitted once, on the first iteration, and after that this object is a
    pass-through with one extra `if` in it.
    """

    def __init__(
        self,
        inner: VoiceModel,
        pcm: bytes,
        rate: int,
        *,
        chunk_seconds: float = CHUNK_SECONDS,
        pace: float = PACE,
    ) -> None:
        self._inner = inner
        self._pcm = pcm
        self._rate = rate
        self._chunk = max(1, int(rate * chunk_seconds)) * BYTES_PER_SAMPLE
        self._gap = chunk_seconds * pace
        self.spoke = False

    @property
    def turn_id(self) -> int:
        return self._inner.turn_id

    async def send_audio(self, pcm: bytes) -> None:
        await self._inner.send_audio(pcm)

    async def end_of_speech(self) -> None:
        await self._inner.end_of_speech()

    @property
    def seconds(self) -> float:
        return bytes_to_seconds(self._pcm, self._rate)

    async def events(self) -> AsyncIterator[Event]:
        # Start the real model's stream FIRST, before a byte of the greeting
        # goes out.
        #
        # An async generator does not run a line of its body until something
        # asks it for a value, and both models do real work on that first
        # pull: CascadeSession starts its mouth pump there, lazily, with
        # `if self._pump is None`. Draining the greeting first and only then
        # iterating the inner stream would leave that undone for the seven
        # seconds the recording plays, with the socket's traffic piling up
        # unread behind it. Asking for the first event now and holding the
        # future makes the wrapper invisible: the model is as live at t=0 as
        # it would be with nothing wrapped around it.
        inner = self._inner.events()
        first = asyncio.ensure_future(inner.__anext__())
        # Scheduling a task does not run it. Without this the loop below
        # never awaits anything real at pace 0, control never returns to the
        # event loop, and the model's first pull - the one that starts the
        # cascade's mouth pump - is deferred to after the greeting after all.
        # One tick, and it is running before a byte goes out.
        await asyncio.sleep(0)

        # The wait goes BEFORE each chunk except the first, not after each
        # one. The first chunk leaves instantly, which is the latency this
        # whole change buys - the caller hears "Vanakkam" with no wait at
        # all - and the turn still ends when the audio ends rather than one
        # chunk later.
        for index, start in enumerate(range(0, len(self._pcm), self._chunk)):
            if index and self._gap:
                await asyncio.sleep(self._gap)
            yield AudioOut(
                turn_id=OPENING_TURN, pcm=self._pcm[start : start + self._chunk]
            )
        # Zero usage, and that is the entire point of the file: this turn
        # was not generated, so there is nothing to bill for it. first_audio
        # is zero rather than None because the audio was instant, which is
        # true and is the other half of what this buys.
        yield TurnComplete(turn_id=OPENING_TURN, usage=TurnUsage(), first_audio_ms=0.0)
        self.spoke = True

        # Whatever the model produced while the greeting was playing comes
        # out here, in order, and then the rest of the call.
        try:
            yield await first
        except StopAsyncIteration:
            return
        async for event in inner:
            yield event
