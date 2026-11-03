"""Barge-in is the behaviour worth testing here; the rest is plumbing."""

from __future__ import annotations

import asyncio
import struct

from src.voice.events import AudioOut, TurnComplete, TurnInterrupted
from src.voice.session import Conversation
from src.voice.usage import TurnUsage
from src.voice.vad import EnergyVad
from tests.fakes import FakeSink, FakeSource

FRAME_SAMPLES = 320
SILENCE = b"\x00" * FRAME_SAMPLES * 2
SPEECH = struct.pack(f"<{FRAME_SAMPLES}h", *([4000, -4000] * (FRAME_SAMPLES // 2)))


class StubModel:
    """Stands in for GeminiLiveSession: replays events, records sent audio.

    `keep_open` models a socket that stays up after the scripted events, which
    is what a real session does. Without it the model pump finishes first and
    `run()` correctly cancels the microphone - correct in production, useless
    in a test that needs the microphone to keep feeding.
    """

    def __init__(self, events: list, keep_open: bool = False) -> None:
        self._events = list(events)
        self._keep_open = keep_open
        self.sent: list[bytes] = []
        self.ends = 0
        self.turn_id = 1

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def end_of_speech(self) -> None:
        # The real adapter advances its turn id here, which is the whole point.
        self.turn_id += 1
        self.ends += 1

    async def events(self):
        for event in self._events:
            yield event
            await asyncio.sleep(0)
        if self._keep_open:
            await asyncio.Event().wait()


async def test_every_microphone_frame_reaches_the_model():
    """Silence included - the server does not bill it and gating risks turn detection."""
    source = FakeSource([SILENCE, SILENCE, SILENCE])
    model = StubModel([])
    await Conversation(model, source, FakeSink()).run()
    assert len(model.sent) == 3


async def test_local_speech_while_the_agent_talks_flushes_playback():
    """The whole point: stop without waiting for the server to agree."""
    source = FakeSource([SILENCE] * 40 + [SPEECH] * 5)
    sink = FakeSink()
    model = StubModel([AudioOut(turn_id=1, pcm=b"agent talking")], keep_open=True)

    conversation = Conversation(
        model, source, sink, vad=EnergyVad(speech_frames_to_trigger=3)
    )
    # Stand the agent outside its own grace window: this is a caller speaking
    # over a reply already in progress, not the agent hearing itself start.
    conversation.BARGE_IN_GRACE_SECONDS = 0.0
    await conversation.run()

    assert sink.flushes >= 1, "playback was not stopped when the caller spoke"
    assert b"agent talking" in b"".join(sink.discarded)


async def test_audio_from_an_abandoned_turn_never_reaches_the_speaker():
    """Late chunks for a turn the caller talked over must be dropped."""
    sink = FakeSink()
    model = StubModel(
        [
            AudioOut(turn_id=1, pcm=b"first"),
            TurnInterrupted(turn_id=1),
            AudioOut(turn_id=1, pcm=b"stale, arrived after the interruption"),
            AudioOut(turn_id=2, pcm=b"the new answer"),
        ]
    )
    await Conversation(model, FakeSource([], keep_open=True), sink).run()

    heard = b"".join(sink.written)
    assert b"stale" not in heard
    assert b"the new answer" in heard


async def test_turn_completion_records_latency_and_the_hidden_thinking_tokens():
    usage = TurnUsage(response_audio=205, thoughts=126)
    model = StubModel([TurnComplete(turn_id=1, usage=usage, first_audio_ms=742.0)])

    result = await Conversation(model, FakeSource([], keep_open=True), FakeSink()).run()

    assert result.latencies() == [742.0]
    record = result.turns[0]
    assert record.response_tokens == 205
    # Thinking is tracked separately because it is billed and does not appear
    # in totalTokenCount.
    assert record.thinking_tokens == 126


async def test_the_agent_does_not_interrupt_itself_the_moment_it_starts():
    """Echo of the agent's own voice must not read as a barge-in.

    The microphone hears the speaker. Browser echo cancellation removes most of
    it and not all, and the remainder is speech-shaped - precisely what the VAD
    exists to detect. Without a grace period the agent flushes its own reply a
    few frames after starting it, and the caller hears a syllable and then
    silence, over and over.
    """
    sink = FakeSink()
    source = FakeSource([SILENCE] * 40 + [SPEECH] * 6, keep_open=False)
    model = StubModel([AudioOut(turn_id=1, pcm=b"the agent answering")], keep_open=True)

    conversation = Conversation(
        model, source, sink, vad=EnergyVad(speech_frames_to_trigger=3)
    )
    await conversation.run()

    # 46 frames at 20 ms is under a second of wall clock, all of it inside the
    # grace window, so nothing should have been flushed.
    assert sink.flushes == 0, "the agent interrupted itself"
    assert b"the agent answering" in b"".join(sink.written)


async def test_a_real_interruption_still_lands_once_the_grace_window_passes():
    sink = FakeSink()
    model = StubModel([AudioOut(turn_id=1, pcm=b"talking")], keep_open=True)
    conversation = Conversation(
        model,
        FakeSource([SILENCE] * 10 + [SPEECH] * 6),
        sink,
        vad=EnergyVad(speech_frames_to_trigger=3),
    )
    conversation.BARGE_IN_GRACE_SECONDS = (
        0.0  # as if the agent had been talking a while
    )
    await conversation.run()

    assert sink.flushes >= 1, "a genuine barge-in was ignored"


async def test_the_end_of_the_caller_s_turn_is_signalled():
    """Without this the turn id never advances, and that is fatal.

    Every reply would be tagged turn 0 for the whole call. The first
    interruption puts turn 0 in the abandoned set, and from then on every chunk
    the model sends is dropped before it reaches the speaker - the agent falls
    silent for good while its transcript keeps scrolling. That is exactly the
    "sometimes it replies, sometimes it does not" this fixes.
    """
    model = StubModel([], keep_open=True)
    # Speech, then enough silence for the VAD to release the turn.
    source = FakeSource([SILENCE] * 5 + [SPEECH] * 10 + [SILENCE] * 40)
    await Conversation(model, source, FakeSink(), vad=EnergyVad()).run()

    assert model.ends == 1, "the caller stopping was never signalled"
    assert model.turn_id == 2, "the turn id did not advance"


async def test_silence_alone_never_signals_a_turn():
    """A quiet line is not a caller who has finished speaking."""
    model = StubModel([], keep_open=True)
    await Conversation(
        model, FakeSource([SILENCE] * 60), FakeSink(), vad=EnergyVad()
    ).run()
    assert model.ends == 0


async def test_each_turn_gets_its_own_id_so_abandoning_one_spares_the_next():
    model = StubModel([], keep_open=True)
    source = FakeSource([SPEECH] * 10 + [SILENCE] * 40 + [SPEECH] * 10 + [SILENCE] * 40)
    await Conversation(model, source, FakeSink(), vad=EnergyVad()).run()
    assert model.ends == 2 and model.turn_id == 3
