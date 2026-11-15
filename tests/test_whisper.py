"""The Groq transcription client.

Most of these are refusals. The client is on nobody's path to an answer, so
every way it can fail has to fail quietly and say why in a line that carries
no key.
"""

from __future__ import annotations

import io
import wave
from typing import Any

import pytest

from src.voice.whisper import (
    MIN_BILLED_SECONDS,
    GroqWhisper,
    WhisperError,
    seconds_of,
    to_wav,
)

RATE = 16000


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * RATE * 2))


def responder(status: int, body: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """A fake transport, and the list it records what it was asked to send."""
    sent: list[dict[str, Any]] = []

    async def post(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        sent.append({"url": url, **kwargs})
        return status, body

    return post, sent


class TestTheWav:
    def test_it_is_a_wav_the_standard_library_can_read_back(self):
        pcm = silence(1.0)
        with wave.open(io.BytesIO(to_wav(pcm, RATE)), "rb") as back:
            assert back.getframerate() == RATE
            assert back.getnchannels() == 1
            assert back.getsampwidth() == 2
            assert back.readframes(back.getnframes()) == pcm

    def test_seconds_of_reads_the_clock_off_the_bytes(self):
        assert seconds_of(silence(2.5), RATE) == pytest.approx(2.5)

    def test_a_rate_of_zero_is_no_seconds_rather_than_a_crash(self):
        assert seconds_of(silence(1.0), 0) == 0.0


class TestWhatItRefuses:
    async def test_an_empty_key(self):
        post, sent = responder(200, {"text": "hi"})
        with pytest.raises(WhisperError):
            await GroqWhisper("", post=post).hear(silence(1.0), rate=RATE)
        # Nothing left the process.
        assert sent == []

    async def test_audio_too_short_to_be_an_utterance(self):
        """A gate opening and closing is not speech, and it bills ten seconds."""
        post, sent = responder(200, {"text": "hi"})
        with pytest.raises(WhisperError):
            await GroqWhisper("k", post=post).hear(silence(0.1), rate=RATE)
        assert sent == []

    async def test_a_refusal_carries_the_status_and_nothing_else(self):
        post, _ = responder(401, {"error": {"message": "Bearer sk-secret-123"}})
        with pytest.raises(WhisperError) as raised:
            await GroqWhisper("sk-secret-123", post=post).hear(silence(1.0), rate=RATE)
        # The body is not pasted in: an error body is allowed to echo a
        # header back, and this message goes to a log.
        assert "sk-secret-123" not in str(raised.value)
        assert "401" in str(raised.value)

    async def test_an_empty_transcript_is_not_a_transcript(self):
        post, _ = responder(200, {"text": "   "})
        with pytest.raises(WhisperError):
            await GroqWhisper("k", post=post).hear(silence(1.0), rate=RATE)


class TestWhatItSends:
    async def test_the_key_is_a_header_and_never_the_url(self):
        post, sent = responder(200, {"text": "sari"})
        await GroqWhisper("sk-secret-123", post=post).hear(silence(1.0), rate=RATE)

        assert "sk-secret-123" not in sent[0]["url"]
        assert sent[0]["headers"]["Authorization"] == "Bearer sk-secret-123"

    async def test_it_names_the_language_rather_than_letting_it_be_guessed(self):
        """Whisper romanises Tamil differently per utterance when left to guess.

        A transcript that changes script halfway through a call cannot be
        compared with the Live one, which is the whole point of the module.
        """
        post, sent = responder(200, {"text": "sari"})
        await GroqWhisper("k", post=post).hear(silence(1.0), rate=RATE)
        assert sent[0]["data"]["language"] == "ta"
        assert sent[0]["data"]["temperature"] == "0"


class TestWhatItReturns:
    async def test_a_transcript_and_what_it_cost(self):
        post, _ = responder(200, {"text": "  enakku phone venum  "})
        heard = await GroqWhisper("k", post=post).hear(silence(4.0), rate=RATE)

        assert heard.text == "enakku phone venum"
        assert heard.seconds == pytest.approx(4.0)
        assert heard.model == "whisper-large-v3-turbo"

    async def test_the_ten_second_floor_is_what_is_billed(self):
        """Groq's minimum per ASR request, and most of what this leg costs.

        The measured caller's utterances average 4.4 s, so the floor is the
        majority of the bill and it is recorded rather than engineered around.
        """
        post, _ = responder(200, {"text": "sari"})
        heard = await GroqWhisper("k", post=post).hear(silence(4.0), rate=RATE)

        assert heard.seconds == pytest.approx(4.0)
        assert heard.billed_seconds == MIN_BILLED_SECONDS
        assert heard.usd == pytest.approx(10 / 3600 * 0.04)

    async def test_a_long_utterance_is_billed_for_what_it_was(self):
        post, _ = responder(200, {"text": "sari"})
        heard = await GroqWhisper("k", post=post).hear(silence(25.0), rate=RATE)
        assert heard.billed_seconds == pytest.approx(25.0)
