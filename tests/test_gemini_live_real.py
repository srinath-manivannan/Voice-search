"""Live checks against the real API. Excluded from the default run.

    .venv/bin/python -m pytest -m live

These spend money - a few cents - and they are the only tests that can catch a
protocol change, a model rename, or a config the server silently ignores. CI
never runs them; run them before a release and after any dependency bump.
"""

from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.config import load
from src.voice.events import AudioOut, Transcript, TurnComplete
from src.voice.gemini_live import GeminiLiveSession

FIXTURES = Path(__file__).parent / "fixtures"
FRAME_MS = 20

pytestmark = pytest.mark.live


def read_pcm(name: str, expected_rate: int) -> bytes:
    with wave.open(str(FIXTURES / name)) as w:
        assert w.getframerate() == expected_rate, f"{name} is not {expected_rate} Hz"
        assert w.getnchannels() == 1, f"{name} is not mono"
        return w.readframes(w.getnframes())


@pytest.fixture
def config():
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    return load("development")


async def one_turn(config, fixture: str) -> list:
    """Stream one utterance at wall-clock pace and collect the reply."""
    frame_bytes = config.capture_sample_rate * FRAME_MS // 1000 * 2
    pcm = read_pcm(fixture, config.capture_sample_rate)

    async with GeminiLiveSession(config) as session:

        async def speak() -> None:
            for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
                await session.send_audio(pcm[i : i + frame_bytes])
                await asyncio.sleep(FRAME_MS / 1000)
            await session.end_of_speech()

        collected: list = []
        speaker = asyncio.create_task(speak())
        async for event in session.events():
            collected.append(event)
            if isinstance(event, TurnComplete):
                break
        await speaker
        return collected


async def test_tamil_turn_returns_audio_within_the_phase1_latency_target(config):
    events = await asyncio.wait_for(one_turn(config, "tamil_short.wav"), timeout=60)

    audio = [e for e in events if isinstance(e, AudioOut)]
    done = next(e for e in events if isinstance(e, TurnComplete))

    assert audio, "no audio came back"
    assert sum(len(e.pcm) for e in audio) > 0

    # Phase 1 completion criterion: p95 <= 2 s. One sample cannot prove p95, but
    # a single turn over 2 s means the target is already out of reach.
    assert done.first_audio_ms is not None
    assert done.first_audio_ms < 2000, f"first audio took {done.first_audio_ms:.0f} ms"


async def test_thoughts_are_reported_even_when_thinking_cannot_be_disabled(config):
    """gemini-3.8-live rejects thinkingConfig and still thinks.

    The wire check is that thoughtsTokenCount came back as a number the
    meter can add, not that it is zero. Zero was the old budget field,
    which this model closes the socket on.
    """
    events = await asyncio.wait_for(one_turn(config, "tamil_short.wav"), timeout=60)
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.usage.thoughts >= 0
    assert done.usage.billable_output >= done.usage.response_audio


async def test_reply_respects_the_word_cap(config):
    """Output audio is the largest cost line, so reply length is a real budget."""
    events = await asyncio.wait_for(one_turn(config, "english_short.wav"), timeout=60)
    text = "".join(e.text for e in events if isinstance(e, Transcript)).strip()

    assert text, "no transcript came back"
    words = len(text.split())
    # The cap is an instruction, not a hard limit, so allow headroom - but a
    # reply at twice the cap means the instruction is not landing.
    assert words <= config.reply_word_cap * 2, f"{words} words: {text!r}"
