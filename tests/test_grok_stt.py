"""xAI's speech to text, as the cascade's ear.

Mostly refusals and mostly about the request. What the model hears is not
something a test can pin - that was measured against the real endpoint and
written into the module docstring - but what we ask it is.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.voice.grok_stt import HOST, GrokEar
from src.voice.whisper import WhisperError

RATE = 16000


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * RATE * 2))


def responder(status: int, body: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def post(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        sent.append({"url": url, **kwargs})
        return status, body

    return post, sent


class TestWhatItRefuses:
    async def test_an_empty_key(self):
        post, sent = responder(200, {"text": "hi"})
        with pytest.raises(WhisperError):
            await GrokEar("", post=post).hear(silence(1.0), rate=RATE)
        assert sent == []

    async def test_audio_too_short_to_be_an_utterance(self):
        post, sent = responder(200, {"text": "hi"})
        with pytest.raises(WhisperError):
            await GrokEar("k", post=post).hear(silence(0.1), rate=RATE)
        assert sent == []

    async def test_a_refusal_carries_the_status_and_not_the_key(self):
        post, _ = responder(401, {"error": "Bearer xai-secret-abcdefghij"})
        with pytest.raises(WhisperError) as raised:
            await GrokEar("xai-secret-abcdefghij", post=post).hear(
                silence(1.0), rate=RATE
            )
        assert "xai-secret-abcdefghij" not in str(raised.value)
        assert "401" in str(raised.value)

    async def test_an_empty_transcript_is_not_a_transcript(self):
        post, _ = responder(200, {"text": "   "})
        with pytest.raises(WhisperError):
            await GrokEar("k", post=post).hear(silence(1.0), rate=RATE)


class TestWhatItSends:
    async def test_the_key_is_a_header_and_never_the_url(self):
        post, sent = responder(200, {"text": "sari"})
        await GrokEar("xai-secret-abcdefghij", post=post).hear(silence(1.0), rate=RATE)

        assert sent[0]["url"] == HOST
        assert "xai-secret" not in sent[0]["url"]
        assert sent[0]["headers"]["Authorization"] == "Bearer xai-secret-abcdefghij"

    async def test_raw_pcm_needs_its_format_and_rate_declared(self):
        """Headerless audio is not guessed at server-side.

        Unlike the Groq path there is no WAV wrapper, so these two fields are
        the only thing saying what the bytes are.
        """
        post, sent = responder(200, {"text": "sari"})
        await GrokEar("k", post=post).hear(silence(1.0), rate=8000)

        assert sent[0]["data"]["audio_format"] == "pcm"
        assert sent[0]["data"]["sample_rate"] == "8000"

    async def test_the_file_is_sent_separately_so_it_stays_last(self):
        """`file` must be the LAST field in the multipart form.

        httpx writes every `data` field before every `files` field, so
        passing them apart is what satisfies that - folding the audio into
        `data` would put it in the middle.
        """
        post, sent = responder(200, {"text": "sari"})
        await GrokEar("k", post=post).hear(silence(1.0), rate=RATE)

        assert "file" in sent[0]["files"]
        assert "file" not in sent[0]["data"]

    async def test_filler_words_are_dropped(self):
        post, sent = responder(200, {"text": "sari"})
        await GrokEar("k", post=post).hear(silence(1.0), rate=RATE)
        assert sent[0]["data"]["filler_words"] == "false"

    async def test_key_terms_go_as_one_repeated_field(self):
        """A list VALUE, not several keys.

        httpx expands a list into one repeated field per item, which is the
        shape the API documents. `keyterm1`, `keyterm2` would have been
        accepted and silently ignored.
        """
        post, sent = responder(200, {"text": "sari"})
        ear = GrokEar("k", keyterms=("Acme", "Redmi"), post=post)
        await ear.hear(silence(1.0), rate=RATE)

        assert sent[0]["data"]["keyterm"] == ["Acme", "Redmi"]

    async def test_key_terms_are_capped_where_the_api_caps_them(self):
        post, sent = responder(200, {"text": "sari"})
        ear = GrokEar("k", keyterms=tuple(f"term{n}" for n in range(150)), post=post)
        await ear.hear(silence(1.0), rate=RATE)

        assert len(sent[0]["data"]["keyterm"]) == 100

    async def test_no_key_terms_means_no_field_at_all(self):
        post, sent = responder(200, {"text": "sari"})
        await GrokEar("k", post=post).hear(silence(1.0), rate=RATE)
        assert "keyterm" not in sent[0]["data"]


class TestWhatItReturns:
    async def test_the_transcript_and_the_duration_the_api_measured(self):
        """Its figure, not ours: the bill and the record of it must agree."""
        post, _ = responder(200, {"text": "  enakku phone venum  ", "duration": 3.42})
        heard = await GrokEar("k", post=post).hear(silence(10.0), rate=RATE)

        assert heard.text == "enakku phone venum"
        assert heard.billed_seconds == pytest.approx(3.42)
        # What we sent, which is not what it measured.
        assert heard.seconds == pytest.approx(10.0)

    async def test_a_missing_duration_falls_back_to_what_was_sent(self):
        post, _ = responder(200, {"text": "sari"})
        heard = await GrokEar("k", post=post).hear(silence(4.0), rate=RATE)
        assert heard.billed_seconds == pytest.approx(4.0)

    async def test_tamil_script_comes_back_untouched(self):
        """The ear writes Tamil script and the module does not romanise it.

        Measured against the live endpoint: a budget arrives as a WORD -
        இருபதாயிரம், with no digit in it - which the prefetch's regular
        expression cannot read and the brain can. Normalising here would hide
        which of those two is being relied on.
        """
        said = "எனக்கு இருபதாயிரம் குள்ள ஒரு போன் வேணும்"
        post, _ = responder(200, {"text": said})
        heard = await GrokEar("k", post=post).hear(silence(2.0), rate=RATE)
        assert heard.text == said
