"""xAI's speech to text, on the key this project already has.

The cascade's ear was built against Groq's Whisper because I believed xAI
had no transcription endpoint. That was wrong: xAI launched standalone STT
and TTS APIs in April 2026, `grok-voice-transcribe-2.0` is the current model,
and `XAI_API_KEY` - already set for the summariser - reaches it. So the
cascade needs no new vendor and no second account.

`src/voice/whisper.py` is still here and still works. It is a different
company one letter away, and the two are now genuinely interchangeable behind
the `Ear` protocol, which is the only reason keeping both costs nothing.

## Why raw PCM and no WAV

`/v1/stt` takes `audio_format=pcm` with a `sample_rate`, which is exactly
what comes off the microphone - so unlike the Groq path there is no WAV
header to write and nothing to go wrong in writing one.

**The file must be the last field in the multipart form.** httpx builds
`data` before `files`, so this happens by itself, but it is the kind of
requirement that is invisible until the day a library changes its ordering.

## What it costs

$0.10 an audio hour for batch, published on x.ai. The response carries the
`duration` it actually measured, so that is what is recorded rather than
anything computed here - and unlike Groq's ASR there is no documented
ten-second minimum per request, which on utterances averaging 4.4 seconds is
most of the difference between the two.

The key travels in the Authorization header and nowhere else, for the reason
`src/memory/grok.py` gives: a URL ends up in exception text, access logs and
proxy logs; a header does not.

## It writes Tamil script, and it writes numbers as words

Measured 2026-11-15 by speaking Tanglish through the mouth and transcribing
the audio back:

    sent   Enakku 20000 kulla oru nalla camera phone venum, 25000 varaikkum okay.
    heard  எனக்கு இருபதாயிரம் குள்ள ஒரு நல்ல கேமரா போன் வேணும்,
           இருபத்தி ஐந்தாயிரம் வரைக்கும் ஓகே.

The hearing is excellent - "T Nagar" came back as டீ நகர், "EMI" stayed EMI -
but two things are not what the rest of the project expects.

**It transcribes into Tamil script, not the romanised form the agent speaks
in.** That is a presentation difference and the brain does not care.

**A budget comes back as a word.** `இருபதாயிரம்` is twenty thousand and
there is not a digit in it. `language=en`, `format=true` and omitting the
language entirely were all tried and all returned the same Tamil script with
the same word-numbers: the model detects the language itself, and Inverse
Text Normalisation is documented only for ar, de, en, es, fr, ja, pt, ru,
sv, vi and zh.

### What that does and does not break

`src/agent/prefetch.py` reads a budget with a regular expression over digit
runs. Against this it finds nothing, so **the prefetch cannot fire on the
cascade's ear** until Tamil numerals are parsed - which is a lost
optimisation, not a wrong answer.

The brain is unaffected, which is the finding that matters. Handed that exact
Tamil sentence it called `search_products({"category": "phone",
"max_price": 25000})` and answered *"Poco M8 5G, 24,999 rupees - 45MP
camera. Edhu paakkalaam?"* - the right number, from the word, and a reply
still in Tanglish. A language model reading numerals is not the same problem
as a regular expression reading them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.voice.whisper import Heard, WhisperError, seconds_of

HOST = "https://api.x.ai/v1/stt"

# Published on x.ai: $0.10 an audio hour for batch, $0.20 for streaming.
USD_PER_AUDIO_HOUR = 0.10

# Below this there is no utterance, only a gate opening and closing.
MIN_AUDIO_SECONDS = 0.4

PostAudio = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


async def _post_audio(
    url: str,
    *,
    headers: dict[str, str],
    files: dict[str, Any],
    # A dict, but one whose values may be lists. httpx iterates `data.items()`
    # and expands a list value into one repeated field per item, which is how
    # `keyterm` is sent - the same name several times. Encoding it as
    # keyterm1, keyterm2 would have been accepted and silently ignored.
    data: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, files=files, data=data)
        try:
            body: Any = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return response.status_code, body


class GrokEar:
    """One model, one key, transcription only. Satisfies `cascade.Ear`."""

    def __init__(
        self,
        api_key: str,
        model: str = "grok-voice-transcribe-2.0",
        *,
        language: str = "ta",
        format_numbers: bool = False,
        timeout_s: float = 8.0,
        keyterms: tuple[str, ...] = (),
        post: PostAudio | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language
        self._format = format_numbers
        self._timeout = timeout_s
        # Brand names the catalogue actually carries. The API biases towards
        # them, which is worth doing for exactly the words a mis-hearing
        # costs the most on: a phone nobody stocks cannot be searched for.
        self._keyterms = keyterms
        self._post = post or _post_audio

    @property
    def model(self) -> str:
        return self._model

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        """Transcribe one utterance. Raises WhisperError on any refusal.

        The exception type is the Groq path's, deliberately: the cascade
        catches one thing whichever ear it was given, and inventing a second
        name for "the ear did not answer" would mean two except clauses that
        must never drift apart.
        """
        if not self._api_key:
            raise WhisperError("xai api key is empty")
        seconds = seconds_of(pcm, rate)
        if seconds < MIN_AUDIO_SECONDS:
            raise WhisperError("too little audio to transcribe")

        data: dict[str, Any] = {
            "model": self._model,
            # Raw, headerless audio: the format hint and the rate are
            # required together, and neither is guessed server-side.
            "audio_format": "pcm",
            "sample_rate": str(rate),
            "language": self._language,
            "format": "true" if self._format else "false",
            # "um" and "uh" are not what the caller meant and they are not
            # what the prefetch reads.
            "filler_words": "false",
        }
        if self._keyterms:
            # One list, several fields. Capped where the API caps it.
            data["keyterm"] = [term[:50] for term in self._keyterms[:100]]

        status, body = await self._post(
            HOST,
            headers={"Authorization": f"Bearer {self._api_key}"},
            # `file` must be the LAST field in the form. httpx writes data
            # before files, so passing them separately is what satisfies it.
            files={"file": ("utterance.pcm", pcm, "application/octet-stream")},
            data=data,
            timeout=self._timeout,
        )
        if status >= 400:
            raise WhisperError(f"xai stt returned {status}")

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WhisperError("xai stt returned an empty transcript")

        # The API measures the audio itself. Trusting its figure over ours
        # keeps the bill and the record of the bill from drifting apart.
        measured = body.get("duration")
        billed = float(measured) if isinstance(measured, int | float) else seconds
        return Heard(
            text=text.strip(),
            seconds=seconds,
            billed_seconds=billed,
            model=self._model,
        )
