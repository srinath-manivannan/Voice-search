"""Stop playback when the shopper speaks over it.

The flush is the client's job and has to finish inside 100 ms of the
decision. This records the decision and the event the socket sends.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BargeIn:
    at_ms: int
    flush_ms: int

    @property
    def in_time(self) -> bool:
        return self.flush_ms < 100

    def event(self) -> dict[str, int | str]:
        return {"type": "barge_in", "at_ms": self.at_ms, "flush_ms": self.flush_ms}


def endpoint(speech_ms: int, *, silence_ms: int, hangover_ms: int = 300) -> bool:
    """True when a utterance has ended: speech happened, then silence past the hangover."""
    return speech_ms > 0 and silence_ms >= hangover_ms
