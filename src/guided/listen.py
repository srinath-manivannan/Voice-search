"""Hearing: what a piece of audio says, as text.

Used two ways. Our own speech is played back through it before anyone hears
it - a voice that says 48,996 where the screen says 48,999 is caught here.
And it is the ear for what a customer says, in `hear.py`.

`gemini-3.5-flash-lite` takes audio at its text rate. Measured 2026-11-23:
Tamil correct, 1.4-2.5 s, a paisa or so an utterance.
"""

from __future__ import annotations

import base64
import difflib
import re
from collections.abc import Callable
from typing import Any, Protocol

from src.guided.model import HOST, ModelUnavailable, usd_for

_LETTERS = re.compile(r"[^\w஀-௿]+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

TRANSCRIBE = {
    "ta": (
        "Transcribe exactly what is spoken in this recording, in Tamil script. "
        "Write numbers in digits. Write only the words spoken, nothing else."
    ),
    "en": (
        "Transcribe exactly what is spoken in this recording, in English. "
        "Write numbers in digits. Write only the words spoken, nothing else."
    ),
}


class Poster(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...


_TAMIL = re.compile(r"[\u0b80-\u0bff]")
_LATIN = re.compile(r"[a-zA-Z]")
# A Tamil sentence carries bank names, brands and units in Latin letters,
# so it is not all Tamil script. But it is mostly Tamil: measured on the
# answers that read well, a third to two thirds. The one that read badly -
# the page's English lines copied out with "மற்றும்" between them - was 6%.
TAMIL_SHARE = 0.30


def in_language(text: str, lang: str) -> bool:
    """Is this written in the customer's language, by the share of its letters?"""
    tamil, latin = len(_TAMIL.findall(text)), len(_LATIN.findall(text))
    if tamil + latin < 3:
        return False
    share = tamil / (tamil + latin)
    return share < TAMIL_SHARE if lang == "en" else share >= TAMIL_SHARE


def letters(text: str) -> str:
    return _LETTERS.sub("", text).lower()


def likeness(meant: str, heard: str) -> float:
    """0 to 1, on letters only: punctuation and spacing are not speech."""
    return difflib.SequenceMatcher(None, letters(meant), letters(heard)).ratio()


def figures(text: str) -> frozenset[float]:
    """Every number in a text. 48,999 and 48999 are one figure."""
    plain = re.sub(r"(?<=\d),(?=\d)", "", text)
    return frozenset(float(n) for n in _NUMBER.findall(plain))


def audio_part(wav_bytes: bytes) -> dict[str, Any]:
    return {
        "inlineData": {
            "mimeType": "audio/wav",
            "data": base64.b64encode(wav_bytes).decode(),
        }
    }


def _default_poster() -> Poster:
    import httpx

    return httpx.AsyncClient(timeout=20.0)


class Ear:
    def __init__(
        self,
        api_key: str,
        model: str,
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._poster_factory = poster_factory or _default_poster
        self._poster: Poster | None = None

    async def call(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """One request with audio in it. Returns the body and what it cost."""
        if self._poster is None:
            self._poster = self._poster_factory()
        try:
            response = await self._poster.post(
                f"{HOST}/v1beta/models/{self._model}:generateContent",
                headers={"x-goog-api-key": self._api_key},
                json=body,
            )
        except Exception as failed:  # noqa: BLE001 - any transport failure is the same here
            raise ModelUnavailable(type(failed).__name__) from None
        if response.status_code != 200:
            raise ModelUnavailable(f"the ear returned {response.status_code}")
        reply: dict[str, Any] = response.json()
        usage = reply.get("usageMetadata") or {}
        usd = usd_for(
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
            int(usage.get("thoughtsTokenCount") or 0),
        )
        return reply, usd

    async def transcribe(self, wav_bytes: bytes, lang: str = "ta") -> tuple[str, float]:
        reply, usd = await self.call(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            audio_part(wav_bytes),
                            {"text": TRANSCRIBE.get(lang, TRANSCRIBE["ta"])},
                        ],
                    }
                ],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 300},
            }
        )
        try:
            parts = reply["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return "", usd
        return "".join(str(p.get("text") or "") for p in parts).strip(), usd
