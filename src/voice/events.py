"""What the model layer emits upward.

These are the only vocabulary the session layer needs, which is what keeps the
provider swappable: nothing above this file mentions Gemini, websockets or JSON.

Every event carries `turn_id`. That is not decoration - it is how barge-in stays
correct. When a caller interrupts, audio for the abandoned turn may still be in
flight, already decoded, or already queued in the output device. Tagging lets the
consumer drop anything whose turn is no longer current instead of playing a
sentence the customer already talked over.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.voice.usage import TurnUsage


@dataclass(frozen=True)
class AudioOut:
    """A chunk of the model's speech, PCM at the model's output rate."""

    turn_id: int
    pcm: bytes


@dataclass(frozen=True)
class Transcript:
    """Text of what the model is saying. Useful for logs and evaluation."""

    turn_id: int
    text: str


@dataclass(frozen=True)
class CallerTranscript:
    """What the caller said, one string, emitted when their turn ends.

    Partials are buffered below this layer. The consumer records the
    finished line. Nothing here is awaited, so it cannot sit in front of
    the first audio byte.
    """

    turn_id: int
    text: str


@dataclass(frozen=True)
class TurnInterrupted:
    """The server detected the caller speaking over the model.

    The consumer must stop playback and discard buffered audio for this turn.
    """

    turn_id: int


@dataclass(frozen=True)
class TurnComplete:
    """The model finished speaking.

    `first_audio_ms` is measured from end-of-speech to the first audio byte -
    the number the caller actually experiences as responsiveness, and the one
    Phase 1 is held to (p50 <= 1s, p95 <= 2s).
    """

    turn_id: int
    usage: TurnUsage
    first_audio_ms: float | None


Event = AudioOut | Transcript | CallerTranscript | TurnInterrupted | TurnComplete
