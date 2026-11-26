"""Whisper on Groq, as a second opinion on what the caller said.

Not Grok. **Groq** - a different company, a different key, a different host.
The two names differ by one letter and the project now talks to both, so
`GROQ_API_KEY` is the transcriber and `XAI_API_KEY` is the summariser, and
neither is a fallback for the other.

This exists to be measured, not to be believed. The Live API is still the ear
the call runs on; this one listens to the same frames and says what it heard,
so that [docs/CASCADE.md](../../docs/CASCADE.md) step 3 - moving the
conversation off native audio entirely - can be decided on two transcripts of
real calls rather than on a rate card.

The key travels in the Authorization header and nowhere else, for the reason
`src/memory/grok.py` gives: a URL ends up in exception text, access logs and
proxy logs; a header does not.

## The ten-second floor

Groq bills a **minimum of ten seconds per request** for its ASR models. The
measured caller's utterances average 4.4 seconds, so more than half of what
this leg costs is that floor rather than speech. At $0.04 an audio hour it is
still about 7 paise a minute, which is why the floor is recorded per request
rather than engineered around.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

HOST = "https://api.groq.com/openai/v1/audio/transcriptions"

# Groq charges at least this much per ASR request, whatever was sent.
MIN_BILLED_SECONDS = 10.0

# Published on groq.com/pricing, read 2026-11-15. whisper-large-v3 is
# $0.111 and is not worth 2.8x for a second opinion.
USD_PER_AUDIO_HOUR = 0.04

# What the browser captures and what the Live socket is told it is getting.
# PCM from the microphone is signed 16-bit, one channel.
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# Below this there is no utterance to transcribe, only a gate opening and
# closing. Sending it would buy a ten-second bill for nothing.
MIN_AUDIO_SECONDS = 0.4

# What `hear` is allowed to hand the transport. Narrow on purpose, so a test
# cannot pass a client that logs its headers.
PostAudio = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


class WhisperError(RuntimeError):
    """The transcription did not happen.

    The message is a status or a short reason. It never includes the request
    URL, the headers, or the key - an error body is allowed to echo a header
    back, so the body is not pasted into it.
    """


@dataclass(frozen=True)
class Heard:
    """One utterance, as Groq heard it, and what it cost to ask."""

    text: str
    # Audio actually sent.
    seconds: float
    # What Groq charges for, which is never less than MIN_BILLED_SECONDS.
    billed_seconds: float
    model: str

    @property
    def usd(self) -> float:
        return self.billed_seconds / 3600 * USD_PER_AUDIO_HOUR


def seconds_of(pcm: bytes, rate: int) -> float:
    """How long a run of raw PCM plays for."""
    if rate <= 0:
        return 0.0
    return len(pcm) / (rate * SAMPLE_WIDTH_BYTES * CHANNELS)


def to_wav(pcm: bytes, rate: int) -> bytes:
    """Raw capture PCM wrapped in a WAV header.

    Groq takes a file, not a byte stream, and will not guess at a sample
    rate. `wave` is in the standard library and writes 44 bytes of header, so
    this adds no dependency and nothing worth measuring to the request.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH_BYTES)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


async def _post_audio(
    url: str,
    *,
    headers: dict[str, str],
    files: dict[str, Any],
    data: dict[str, str],
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


class GroqWhisper:
    """One model, one key, transcription only."""

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-large-v3-turbo",
        *,
        timeout_s: float = 8.0,
        post: PostAudio | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._post = post or _post_audio

    @property
    def model(self) -> str:
        return self._model

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        """Transcribe one utterance. Raises WhisperError on any refusal.

        `temperature=0` because this is a measurement: a second opinion that
        varies between runs cannot be compared with anything, including
        itself.
        """
        if not self._api_key:
            raise WhisperError("groq api key is empty")
        seconds = seconds_of(pcm, rate)
        if seconds < MIN_AUDIO_SECONDS:
            raise WhisperError("too little audio to transcribe")

        status, body = await self._post(
            HOST,
            headers={"Authorization": f"Bearer {self._api_key}"},
            files={"file": ("utterance.wav", to_wav(pcm, rate), "audio/wav")},
            data={
                "model": self._model,
                # Tamil. The caller speaks Tamil or Tanglish and Whisper
                # romanises Tamil far more consistently when it is told which
                # language it is listening to than when it guesses per
                # utterance - and a transcript that changes script halfway
                # through cannot be compared with the Live one.
                "language": "ta",
                "response_format": "json",
                "temperature": "0",
            },
            timeout=self._timeout,
        )
        if status >= 400:
            raise WhisperError(f"groq returned {status}")

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WhisperError("groq returned an empty transcript")
        return Heard(
            text=text.strip(),
            seconds=seconds,
            billed_seconds=max(seconds, MIN_BILLED_SECONDS),
            model=self._model,
        )
