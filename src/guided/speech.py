"""Speaking: one voice for everything the guided call says.

The owner chose `ta-in-techagent-5` on 2026-11-24, by ear, from four
auditions. It speaks Tamil and English, so the fixed lines (recorded once,
`make guided-voice`) and the sentences built from live data (spoken here,
on demand) are one speaker.

## A sentence is heard by us before it is heard by anyone

The auditions were read back, and the read-back of "48,999 ரூபாய்" came
back as 49,999 once and 48,996 once. Whether the voice or the ear slipped,
a wrong price spoken aloud is the one error this product cannot make. So a
sentence is synthesized, transcribed, and played only if what was heard is
the sentence (letters 0.85 alike) **and carries every figure in it**. A
sentence that fails is never played: the screen already shows it, and the
customer reads the price instead of hearing a wrong one.

It costs a second or two and a paisa, once: the checked audio is kept on
disk under the hash of the voice and the exact text, so the next customer
who is told "Poco M8 5G, 24,999 rupees" is told it for nothing.

## What it costs

`gemini-3.8-flash-lite-tts` charges audio out by the token, 25 a second:
$6 a million until 2026-12-31 and **$12 from 2027-01-01**, which is why the
rate here takes the date. Every new sentence is admitted by the governor
against the most it can cost, before anything is generated.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from src.guided.listen import figures, likeness
from src.guided.model import HOST, ModelUnavailable
from src.guided.voice import wav

log = logging.getLogger(__name__)

SPEAKER_MODEL = "gemini-3.8-flash-lite-tts"
VOICE = "ta-in-techagent-5"
RATE = 24_000
TOKENS_PER_SECOND = 25

# The longest sentence spoken. A result list of three phones with prices
# runs about 130 characters and 13 seconds; this is room for that and a
# little more, and a hard bound on what one sentence can cost.
MOST_CHARS = 260
MOST_AUDIO_SECONDS = 30
# Below this alike, or missing a figure that matters, the sentence is not
# played. Loose on letters on purpose: the ear writes "Samsung Galaxy" as
# "சாம்சங் கேலக்ஸி" about as often as not, which is the same words and
# scores 0.67. What this catches is a different sentence. The figures are
# the strict half - a price must be heard exactly as written.
ALIKE_ENOUGH = 0.5
# A sentence that failed its check is not tried again for this long, so a
# shelf whose price the voice cannot say does not pay to fail per customer.
FORGET_FAILURE_SECONDS = 600.0
MOST_FILES = 1_500

PRICE_CHANGE = date(2027, 1, 1)


@dataclass(frozen=True)
class TtsRates:
    """USD per 1M tokens."""

    text_in: float
    audio_out: float


def tts_rates(on: date | None = None) -> TtsRates:
    """The speech model's card, on a given day. It doubles on 2027-01-01."""
    day = on or date.today()
    return TtsRates(1.00, 12.00) if day >= PRICE_CHANGE else TtsRates(0.50, 6.00)


def tts_usd(prompt_tokens: int, audio_tokens: int, on: date | None = None) -> float:
    rates = tts_rates(on)
    return (prompt_tokens * rates.text_in + audio_tokens * rates.audio_out) / 1e6


def worst_case_usd(on: date | None = None) -> float:
    """The most one sentence can cost: the longest audio, and hearing it back."""
    audio_tokens = MOST_AUDIO_SECONDS * TOKENS_PER_SECOND
    # Hearing it back: the same audio in at the text rate, a short reply.
    hear = (audio_tokens * 0.30 + 300 * 2.50) / 1e6
    return tts_usd(MOST_CHARS, audio_tokens, on) + hear


class Poster(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...


class Mouth(Protocol):
    async def say(self, text: str) -> tuple[bytes, float]: ...


class Hears(Protocol):
    async def transcribe(
        self, wav_bytes: bytes, lang: str = ...
    ) -> tuple[str, float]: ...


def _default_poster() -> Poster:
    import httpx

    return httpx.AsyncClient(timeout=30.0)


class Speaker:
    """One sentence in, PCM out. No instruction, no style: the text alone.

    A style instruction ("speak softly and warmly") was tried in the
    auditions, and on the English line the model read the instruction out
    loud as part of the sentence.
    """

    def __init__(
        self,
        api_key: str,
        voice: str = VOICE,
        model: str = SPEAKER_MODEL,
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._poster_factory = poster_factory or _default_poster
        self._poster: Poster | None = None

    @property
    def voice(self) -> str:
        return self._voice

    async def say(self, text: str) -> tuple[bytes, float]:
        if self._poster is None:
            self._poster = self._poster_factory()
        try:
            response = await self._poster.post(
                f"{HOST}/v1beta/models/{self._model}:generateContent",
                headers={"x-goog-api-key": self._api_key},
                json={
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": self._voice}
                            }
                        },
                    },
                },
            )
        except Exception as failed:  # noqa: BLE001 - any transport failure is the same here
            raise ModelUnavailable(type(failed).__name__) from None
        if response.status_code != 200:
            raise ModelUnavailable(f"the voice returned {response.status_code}")
        body = response.json()
        usage = body.get("usageMetadata") or {}
        pcm = b""
        for candidate in body.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                if data := (part.get("inlineData") or {}).get("data"):
                    pcm += base64.b64decode(data)
        usd = tts_usd(
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
        )
        return pcm_from(pcm), usd


def pcm_from(payload: bytes) -> bytes:
    """Pure 16-bit PCM out of what the speech model actually sends.

    `generateContent` answers with `audio/wav` - a 44-byte RIFF header on
    the first part, and sometimes headers on later parts too. Treating the
    whole payload as PCM wrapped those headers INSIDE our own WAV, where
    every one of them played as a click: the owner heard his questions
    open "டக்"-ன்னு, like a TV losing signal. So every embedded header is
    stripped, wherever it sits, and only samples remain.
    """
    out = bytearray()
    at = 0
    while at < len(payload):
        if payload[at : at + 4] == b"RIFF" and payload[at + 8 : at + 12] == b"WAVE":
            at += 44
            continue
        stop = payload.find(b"RIFF", at + 1)
        while stop != -1 and payload[stop + 8 : stop + 12] != b"WAVE":
            stop = payload.find(b"RIFF", stop + 1)
        out += payload[at : stop if stop != -1 else len(payload)]
        at = stop if stop != -1 else len(payload)
    return bytes(out)


def heard_as_said(
    meant: str, heard: str, must_hear: frozenset[float] | None = None
) -> bool:
    """Was this the sentence, with every figure that matters in it?"""
    needed = figures(meant) if must_hear is None else must_hear
    return likeness(meant, heard) >= ALIKE_ENOUGH and needed <= figures(heard)


@dataclass
class _Failed:
    at: float


class SpeechBook:
    """Sentences, spoken once, checked once, kept.

    `request` decides whether a sentence will be spoken and returns its key
    at once; the audio is made behind it. `fetch` waits for it. The page
    shows what was said immediately and plays the voice when it lands, so a
    customer is never held up by a sentence being generated.
    """

    def __init__(
        self,
        mouth: Mouth,
        ear: Hears,
        folder: Path,
        voice: str = VOICE,
        most_files: int = MOST_FILES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mouth = mouth
        self._ear = ear
        self._folder = folder
        self._voice = voice
        self._most = most_files
        self._clock = clock
        self._making: dict[str, asyncio.Task[bool]] = {}
        self._failed: dict[str, _Failed] = {}
        self._kept: OrderedDict[str, None] = OrderedDict()
        if folder.is_dir():
            for path in sorted(folder.glob("*.wav"), key=lambda p: p.stat().st_mtime):
                self._kept[path.stem] = None

    def key(self, text: str) -> str:
        return hashlib.sha256(f"{self._voice}\n{text}".encode()).hexdigest()[:32]

    def path(self, key: str) -> Path:
        return self._folder / f"{key}.wav"

    def request(
        self,
        text: str,
        lang: str,
        may_spend: Callable[[float], bool],
        charge: Callable[[float], None],
        must_hear: frozenset[float] | None = None,
    ) -> str | None:
        """The key this sentence will be playable under, or None if it won't be."""
        text = text.strip()
        if not text or len(text) > MOST_CHARS:
            return None
        key = self.key(text)
        if key in self._kept:
            self._kept.move_to_end(key)
            return key
        if key in self._making:
            return key
        failed = self._failed.get(key)
        if failed is not None and self._clock() - failed.at < FORGET_FAILURE_SECONDS:
            return None
        if not may_spend(worst_case_usd()):
            return None
        task = asyncio.ensure_future(self._make(key, text, lang, charge, must_hear))
        self._making[key] = task
        task.add_done_callback(functools.partial(self._done_making, key))
        return key

    def _done_making(self, key: str, _task: asyncio.Task[bool]) -> None:
        self._making.pop(key, None)

    async def fetch(self, key: str, patience_s: float = 25.0) -> bytes | None:
        """The checked audio, as WAV, or None if there is none to play."""
        if key in self._kept and self.path(key).is_file():
            return self.path(key).read_bytes()
        task = self._making.get(key)
        if task is None:
            return None
        try:
            ok = await asyncio.wait_for(asyncio.shield(task), patience_s)
        except asyncio.TimeoutError:
            return None
        return self.path(key).read_bytes() if ok and self.path(key).is_file() else None

    async def _make(
        self,
        key: str,
        text: str,
        lang: str,
        charge: Callable[[float], None],
        must_hear: frozenset[float] | None = None,
    ) -> bool:
        spent = 0.0
        try:
            pcm, usd = await self._mouth.say(text)
            spent += usd
            if not pcm:
                raise ModelUnavailable("no audio came back")
            recording = wav(pcm, RATE)
            heard, cost = await self._ear.transcribe(recording, lang)
            spent += cost
        except ModelUnavailable as down:
            log.info("a sentence could not be spoken: %s", down)
            self._failed[key] = _Failed(self._clock())
            return False
        finally:
            charge(spent)
        if not heard_as_said(text, heard, must_hear):
            log.warning("a sentence was not played - it was heard as something else")
            self._failed[key] = _Failed(self._clock())
            return False
        self._folder.mkdir(parents=True, exist_ok=True)
        self.path(key).write_bytes(recording)
        self._kept[key] = None
        while len(self._kept) > self._most:
            old, _ = self._kept.popitem(last=False)
            self.path(old).unlink(missing_ok=True)
        return True
