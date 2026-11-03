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
        self.turn_id = 1

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

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
