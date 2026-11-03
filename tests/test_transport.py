"""The transport seam is what makes Phase 2 cheap, so its shape is asserted."""

from __future__ import annotations

import pytest

from src.voice.local_audio import MicrophoneSource, SpeakerSink
from src.voice.transport import AudioSink, AudioSource
from tests.fakes import FakeSink, FakeSource


def test_local_devices_satisfy_the_protocols():
    # If this breaks, Phase 2's telephony transport has a moving target.
    assert isinstance(MicrophoneSource(16000), AudioSource)
    assert isinstance(SpeakerSink(24000), AudioSink)


def test_fakes_satisfy_the_same_protocols():
    assert isinstance(FakeSource([]), AudioSource)
    assert isinstance(FakeSink(), AudioSink)


async def test_source_yields_every_frame_then_stops():
    source = FakeSource([b"a", b"b", b"c"])
    assert [f async for f in source.frames()] == [b"a", b"b", b"c"]


async def test_flush_discards_unheard_audio_rather_than_playing_it():
    sink = FakeSink()
    await sink.write(b"first half of a sentence")
    await sink.flush()
    await sink.write(b"after the interruption")

    # The point of barge-in: what was queued never reaches the caller.
    assert sink.discarded == [b"first half of a sentence"]
    assert sink.written == [b"after the interruption"]
    assert sink.flushes == 1


@pytest.mark.parametrize("rate,expected", [(16000, 640), (24000, 960), (8000, 320)])
def test_frame_size_matches_20ms_of_16bit_mono(rate, expected):
    from src.voice.local_audio import _frame_bytes

    # 8000 is the telephony rate - included so the arithmetic is already right
    # when Phase 2 arrives.
    assert _frame_bytes(rate) == expected
