"""Real device tests. Excluded by default - CI runners have no sound card.

.venv/bin/python -m pytest -m audio
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.voice.local_audio import SpeakerSink

pytestmark = pytest.mark.audio


async def test_flush_stops_playback_well_inside_the_barge_in_target():
    """Queue three seconds, flush, and prove it did not play them.

    Phase 0 set the barge-in target at about 300 ms from detection to silence.
    This measures the part we control: how long `flush` takes to abandon
    audio already handed to the operating system.
    """
    sink = SpeakerSink(24000)
    three_seconds = b"\x00" * (24000 * 2 * 3)
    await sink.write(three_seconds)

    started = time.perf_counter()
    await sink.flush()
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 300, f"flush took {elapsed_ms:.0f} ms"
    await sink.close()


async def test_speaker_recovers_after_a_flush():
    """A barge-in must not leave the agent mute for the rest of the call."""
    sink = SpeakerSink(24000)
    await sink.write(b"\x00" * 4800)
    await sink.flush()
    await sink.write(b"\x00" * 4800)  # must transparently respawn the player
    await asyncio.sleep(0.1)
    await sink.close()
