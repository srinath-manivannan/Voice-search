"""Tamil speech, cached by the voice and the sentence.

A line that has been synthesised is served again from
``/api/v1/guided/speech/{sha256}.wav``. When nothing has been synthesised,
the screen keeps the text and ``spoken`` is false. The device voice is not
a fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def cache_key(voice: str, text: str) -> str:
    return hashlib.sha256(f"{voice}\n{text}".encode()).hexdigest()


@dataclass(frozen=True)
class SpeechPlan:
    key: str
    spoken: bool
    cached: bool
    fallback: str = ""


def prepare(text: str, voice: str, directory: Path) -> SpeechPlan:
    """Look up a cached clip. Do not synthesise and do not call a device voice."""
    key = cache_key(voice, text)
    path = directory / f"{key}.wav"
    if path.is_file() and path.stat().st_size > 0:
        return SpeechPlan(key, spoken=True, cached=True)
    return SpeechPlan(key, spoken=False, cached=False, fallback="details_on_screen")


def chunks(text: str) -> list[str]:
    """Sentence pieces, so a long reply can start on the first one."""
    parts = [part.strip() for part in text.replace("?", ".").split(".")]
    return [part for part in parts if part]
