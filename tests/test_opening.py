"""The greeting that is played instead of generated.

The saving is easy to state and easy to get wrong: a recording is only free
if nothing downstream can tell it apart from a generated turn. `session.py`
holds the floor, gates the microphone, protects the opening from barge-in and
drains the last line on the strength of `AudioOut` then `TurnComplete` - so
those are what these tests are about, not the audio.
"""

from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.voice.events import AudioOut, CallerTranscript, Event, TurnComplete
from src.voice.opening import (
    CHUNK_SECONDS,
    OPENING_TURN,
    RECORDING,
    SpeaksTheOpening,
    bytes_to_seconds,
    load_recording,
)
from src.voice.usage import TurnUsage
from src.voice.vad import EnergyVad

RATE = 24000


def speech(seconds: float) -> bytes:
    """Bytes that are not silence, so a gate that drops silence keeps them."""
    samples = int(seconds * RATE)
    return b"".join(
        ((i % 2000) - 1000).to_bytes(2, "little", signed=True) for i in range(samples)
    )


class FakeModel:
    """The real session's surface, and a record of what was asked of it."""

    def __init__(self, *events: Event) -> None:
        self._events = list(events)
        self.sent: list[bytes] = []
        self.ends = 0
        self.turn_id = 7

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def end_of_speech(self) -> None:
        self.ends += 1

    async def events(self) -> AsyncIterator[Event]:
        for event in self._events:
            yield event


def opening(pcm: bytes, inner: FakeModel | None = None) -> SpeaksTheOpening:
    # pace=0 so the tests do not sit through the greeting in real time. The
    # default is deliberately not zero; see the note on PACE.
    return SpeaksTheOpening(inner or FakeModel(), pcm, RATE, pace=0.0)


class TestTheShapeItEmits:
    async def test_audio_then_a_turn_that_completes(self):
        """The exact sequence a generated greeting produced.

        Everything in session.py that lets go of the floor runs through a
        TurnComplete. A trace once caught the greeting's socket dying before
        one arrived: the gate stayed shut for thirty seconds while the caller
        talked into nothing. A recording that forgot to end its turn would
        reproduce that on every single call.
        """
        out = [event async for event in opening(speech(1.0)).events()]

        assert all(isinstance(e, AudioOut) for e in out[:-1])
        assert isinstance(out[-1], TurnComplete)

    async def test_the_whole_recording_comes_out_once_and_in_order(self):
        pcm = speech(1.0)
        out = [e async for e in opening(pcm).events() if isinstance(e, AudioOut)]

        assert b"".join(chunk.pcm for chunk in out) == pcm

    async def test_it_is_chunked_rather_than_sent_in_one_piece(self):
        """The browser's queue is a fixed size and was tuned for the model's
        chunking. One five-second frame is not what it was built for."""
        out = [
            e async for e in opening(speech(2.0)).events() if isinstance(e, AudioOut)
        ]

        assert len(out) > 1
        assert all(chunk.pcm for chunk in out)

    async def test_the_turn_is_billed_at_nothing(self):
        """The entire point of the file. If this ever reports usage, the
        cost meter will show a charge for a turn nobody was charged for."""
        complete = [
            e
            async for e in opening(speech(1.0)).events()
            if isinstance(e, TurnComplete)
        ][0]

        assert complete.usage == TurnUsage()
        assert complete.first_audio_ms == 0.0

    async def test_every_event_carries_the_opening_turn_id(self):
        out = [e async for e in opening(speech(1.0)).events()]

        assert {e.turn_id for e in out} == {OPENING_TURN}

    async def test_the_opening_turn_cannot_collide_with_a_real_one(self):
        """A live session increments its turn id BEFORE emitting a turn, so
        its first is 1. Were the greeting to share an id with a later turn,
        an interruption could retroactively discard it."""
        assert OPENING_TURN == 0


class TestWhatItPassesThrough:
    async def test_the_model_s_own_events_follow_the_greeting(self):
        later = CallerTranscript(turn_id=1, text="enakku phone venum")
        out = [e async for e in opening(speech(0.5), FakeModel(later)).events()]

        assert out[-1] == later
        assert isinstance(out[-2], TurnComplete)

    async def test_audio_from_the_caller_reaches_the_real_model(self):
        inner = FakeModel()
        wrapper = opening(speech(0.5), inner)

        await wrapper.send_audio(b"frame")
        await wrapper.end_of_speech()

        assert inner.sent == [b"frame"]
        assert inner.ends == 1

    async def test_the_turn_id_is_the_real_model_s(self):
        """Conversation reads this to discard abandoned audio. It has to be
        the live session's view, not the wrapper's."""
        assert opening(speech(0.5), FakeModel()).turn_id == 7


class TestTheRecordingOnDisk:
    def test_the_one_that_ships_is_there_and_is_a_greeting(self):
        """Not a fixture - the actual file every call will play.

        Length is the check that matters. The recorder refuses anything
        outside two to fifteen seconds, and this is the same question asked
        of what was committed rather than of what was captured.
        """
        pcm = load_recording()
        assert pcm is not None, f"{RECORDING} is missing - run `make greeting`"
        assert 2.0 < bytes_to_seconds(pcm, RATE) < 15.0

    def test_a_missing_recording_is_a_fallback_not_a_crash(self):
        """Deleting the file has to be a safe way to undo this, and a fresh
        clone has no recording until `make greeting` is run."""
        assert load_recording(Path("/nonexistent/greeting.pcm")) is None

    def test_an_empty_file_is_also_a_fallback(self, tmp_path: Path):
        empty = tmp_path / "greeting.pcm"
        empty.write_bytes(b"")
        assert load_recording(empty) is None


class TestTheArithmetic:
    @pytest.mark.parametrize(
        ("pcm_bytes", "rate", "seconds"),
        [
            (24000 * 2, 24000, 1.0),
            (0, 24000, 0.0),
            (16000 * 2 * 3, 16000, 3.0),
            (100, 0, 0.0),  # a rate of zero is a config error, not a crash
        ],
    )
    def test_how_long_it_will_play(self, pcm_bytes: int, rate: int, seconds: float):
        assert bytes_to_seconds(bytes(pcm_bytes), rate) == seconds


class TestThroughTheRealConversation:
    """The risk this change actually carries, exercised through session.py.

    Everything above tests the wrapper in isolation. What matters on a call
    is what `Conversation` does with it: it claims the floor before the first
    chunk exists, shuts the microphone while the agent speaks, protects the
    opening from barge-in, and lets go on TurnComplete. A recording that got
    any of that wrong would hold the gate shut for thirty seconds with the
    page saying "listening" - which is a trace this project has already seen
    once, from the generated greeting's socket dying.
    """

    async def test_the_recording_reaches_the_caller(self):
        from src.voice.session import Conversation
        from tests.fakes import FakeSink, FakeSource
        from tests.test_session import StubModel

        pcm = speech(0.4)
        inner = StubModel([], keep_open=False)
        sink = FakeSink()

        await Conversation(
            SpeaksTheOpening(inner, pcm, RATE, pace=0.0),
            FakeSource([]),
            sink,
            agent_opens=True,
        ).run()

        assert b"".join(sink.written + sink.discarded) == pcm

    async def test_the_floor_is_given_back_when_the_recording_ends(self):
        """The caller's first turn has to be heard, and only after the line.

        `agent_opens=True` claims the floor before any audio exists, and the
        only thing that gives it back is a TurnComplete. Asserting that
        frames reached the model would prove nothing - every frame reaches
        the model, gate or no gate, because gating them risks the server's
        turn detection. What the gate actually holds back is the decision
        that the caller has FINISHED, so that is what is asserted here.

        Deleting the TurnComplete from `events` makes this fail: the floor
        is never released, `end_of_speech` is never signalled, and the call
        is the thirty-second dead line this project has already seen once.
        """
        from src.voice.session import Conversation
        from tests.fakes import FakeSink, FakeSource
        from tests.test_session import SILENCE, SPEECH, StubModel

        inner = StubModel([], keep_open=True)
        # A real syllable after the greeting, then enough silence to end it.
        source = FakeSource([SILENCE] * 5 + [SPEECH] * 12 + [SILENCE] * 40)

        await Conversation(
            SpeaksTheOpening(inner, speech(0.2), RATE, pace=0.0),
            source,
            FakeSink(),
            agent_opens=True,
            vad=EnergyVad(),
        ).run()

        assert inner.ends == 1, (
            "the caller finished speaking and the model was never told - "
            "the floor was not given back when the recording ended"
        )

    async def test_the_greeting_is_not_billed_into_the_call(self):
        """A recorded opening must not show up in the cost meter.

        `Conversation` records a turn per TurnComplete, so the greeting is
        still a turn in the trace - it has to be, or the floor logic has
        nothing to hang on. It is its zero usage that keeps it free.
        """
        from src.voice.session import Conversation
        from tests.fakes import FakeSink, FakeSource
        from tests.test_session import StubModel

        talking = Conversation(
            SpeaksTheOpening(StubModel([]), speech(0.2), RATE, pace=0.0),
            FakeSource([]),
            FakeSink(),
            agent_opens=True,
        )
        result = await talking.run()

        assert [t.turn_id for t in result.turns] == [OPENING_TURN]
        assert result.turns[0].response_tokens == 0
        assert result.turns[0].thinking_tokens == 0
        assert result.turns[0].prompt_audio_tokens == 0
        assert result.turns[0].prompt_text_tokens == 0


class TestItIsPacedLikeSpeech:
    """The bug that made the first real call say nothing, in one assertion.

    TurnComplete is what gives the floor back: it opens the microphone and
    clears the opening's barge-in protection. So it must not arrive while the
    browser is still playing the greeting out loud - the microphone then
    hears the agent's own voice, calls it the caller's turn, and hands the
    model a turn made of its own greeting.

    Measured on the traced calls of 2026-11-15 and 2026-11-16, both about
    seven seconds of audio:

        generated   first chunk 1512 ms, turn_complete 8380 ms
        recorded    first chunk  907 ms, turn_complete 2780 ms

    The recording was emitted at four times real time, so the floor came back
    5.1 seconds early, end_of_speech fired at 4.5 s on the echo, and the call
    produced one turn, zero tokens and Rs 0.00.

    This is a real-clock test on purpose. The shape tests all pass pace=0.0,
    which is exactly the setting that hid this.
    """

    async def test_the_recording_takes_about_as_long_to_emit_as_to_play(self):
        import time

        pcm = speech(1.0)
        # The real default pace, not the one the other tests use.
        wrapper = SpeaksTheOpening(FakeModel(), pcm, RATE)

        started = time.monotonic()
        async for event in wrapper.events():
            if isinstance(event, TurnComplete):
                break
        elapsed = time.monotonic() - started

        playing = bytes_to_seconds(pcm, RATE)
        # One chunk of lead is fine and is what the model does too: it stops
        # sending a moment before playback ends. Four times real time is not.
        assert elapsed > playing - CHUNK_SECONDS - 0.05, (
            f"emitted {playing:.2f}s of audio in {elapsed:.2f}s - the floor "
            f"comes back while the greeting is still being heard"
        )

    async def test_the_first_chunk_does_not_wait(self):
        """The latency this change buys. Pacing must not delay the start."""
        import time

        wrapper = SpeaksTheOpening(FakeModel(), speech(1.0), RATE)

        started = time.monotonic()
        async for _ in wrapper.events():
            break
        assert time.monotonic() - started < 0.05


class TestTheInnerStreamIsLiveFromTheStart:
    """The wrapper must not delay the model it wraps.

    An async generator runs no part of its body until something asks it for
    a value, and both models do real work on that first pull - CascadeSession
    starts its mouth pump there, lazily. Emitting seven seconds of greeting
    before touching the inner stream would leave that undone for seven
    seconds, with the socket's traffic piling up unread behind it.
    """

    async def test_the_model_is_pulled_before_the_greeting_finishes(self):
        started = asyncio.Event()

        class Lazy:
            turn_id = 1

            async def send_audio(self, pcm: bytes) -> None: ...
            async def end_of_speech(self) -> None: ...

            async def events(self):
                # Whatever a real model does on its first pull. Nothing in
                # this body runs until someone asks.
                started.set()
                await asyncio.sleep(3600)
                yield  # pragma: no cover

        wrapper = SpeaksTheOpening(Lazy(), speech(1.0), RATE, pace=0.0)
        seen_turn_complete = False
        async for event in wrapper.events():
            if isinstance(event, TurnComplete):
                seen_turn_complete = True
                break

        assert seen_turn_complete
        assert started.is_set(), (
            "the model was not pulled until after the greeting - its first-"
            "pull work was left undone for the length of the recording"
        )

    async def test_what_the_model_said_during_the_greeting_still_arrives(self):
        later = CallerTranscript(turn_id=1, text="enakku phone venum")
        out = [e async for e in opening(speech(0.4), FakeModel(later)).events()]

        assert out[-1] == later

    async def test_a_model_whose_stream_ends_at_once_does_not_blow_up(self):
        """StopAsyncIteration from the held future has to end the stream,
        not escape as an error through the middle of a call."""
        out = [e async for e in opening(speech(0.4), FakeModel()).events()]

        assert isinstance(out[-1], TurnComplete)
