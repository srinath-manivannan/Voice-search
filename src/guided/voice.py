"""Which of the guided call's lines have a recording that still says them.

The questions are fixed text, so they are recorded once and played - that is
what makes a spoken question cost nothing. It also creates the one way this
design can lie: a sentence is edited in `script.py`, nobody records it again,
and from then on the screen says the new sentence while the voice says the
old one. Nobody reads a waveform in review.

So a recording is only ever named to a client if the text it was made from
is, character for character, the text being shown. The manifest beside the
recordings holds what each one was made from; anything that no longer
matches is treated as not recorded, and the line is shown without sound
until `make guided-voice` is run again.

The recordings are WAV, unlike the greeting's raw PCM. That one has a single
consumer which already knows the sample rate; these are fetched by a browser
that has to be told.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.guided.script import SPOKEN

log = logging.getLogger(__name__)

RECORDINGS = Path(__file__).resolve().parent / "recordings"
MANIFEST = "manifest.json"

_HEADER_BYTES = 44


def wav(pcm: bytes, rate: int) -> bytes:
    """Signed 16-bit mono PCM with the 44-byte header a browser needs."""
    header = b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little")  # length of the format block
    header += (1).to_bytes(2, "little")  # uncompressed
    header += (1).to_bytes(2, "little")  # mono
    header += rate.to_bytes(4, "little")
    header += (rate * 2).to_bytes(4, "little")  # bytes a second
    header += (2).to_bytes(2, "little")  # bytes a sample
    header += (16).to_bytes(2, "little")  # bits a sample
    header += b"data" + len(pcm).to_bytes(4, "little")
    return header + pcm


def wav_seconds(data: bytes) -> float | None:
    """How long a recording runs, or None if it is not one of ours.

    Only the one shape `wav` writes is accepted. A file that is anything
    else - truncated, another format, an error page saved under the name -
    is not a recording, and saying so here is what stops it being played.
    """
    if len(data) <= _HEADER_BYTES or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt ":
        return None
    mono_pcm = data[20:24] == (1).to_bytes(2, "little") * 2
    rate = int.from_bytes(data[24:28], "little")
    sixteen_bit = data[34:36] == (16).to_bytes(2, "little")
    if not (mono_pcm and sixteen_bit and rate > 0 and data[36:40] == b"data"):
        return None
    return (len(data) - _HEADER_BYTES) / (rate * 2)


def read_manifest(folder: Path = RECORDINGS) -> dict[str, dict[str, Any]]:
    """What each recording was made from. Empty if there is none to read."""
    try:
        held = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def write_manifest(
    entries: dict[str, dict[str, Any]], folder: Path = RECORDINGS
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True)
    (folder / MANIFEST).write_text(text + "\n", encoding="utf-8")


class Recordings:
    """The lines that may be played, decided once when the process starts."""

    def __init__(
        self,
        folder: Path = RECORDINGS,
        lines: Mapping[str, str] = SPOKEN,
        voice: str | None = None,
    ) -> None:
        manifest = read_manifest(folder)
        self._by_text = {text: key for key, text in lines.items()}
        current = set()
        fallback = set()
        for key, text in lines.items():
            entry = manifest.get(key) or {}
            made_from = entry.get("text")
            if voice is not None and entry.get("voice") != voice:
                # Another speaker. Never played: the owner heard a second
                # man's voice mid-call and called it improper (2026-11-24),
                # which outranks the earlier "better than silence". The
                # line is shown on screen until `make guided-voice` records
                # it in the one voice; it is only counted here, for the log.
                if made_from == text:
                    try:
                        if (
                            wav_seconds((folder / f"{key}.wav").read_bytes())
                            is not None
                        ):
                            fallback.add(key)
                    except OSError:
                        pass
                continue
            if made_from != text:
                if made_from is not None:
                    log.warning(
                        "the recording of %s is stale and will not be played", key
                    )
                continue
            try:
                playable = wav_seconds((folder / f"{key}.wav").read_bytes()) is not None
            except OSError:
                playable = False
            if playable:
                current.add(key)
        self.current = frozenset(current)
        self.fallback = frozenset(fallback)
        if fallback:
            log.warning(
                "%d line(s) have only a stand-in voice and are shown without "
                "sound until the next `make guided-voice`: %s",
                len(fallback),
                ", ".join(sorted(fallback)),
            )

    def for_line(self, text: str) -> str | None:
        """The name of this sentence's recording, if one may be played."""
        key = self._by_text.get(text)
        return key if key in self.current else None
