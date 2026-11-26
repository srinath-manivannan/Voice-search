"""Active time, which is not the time the socket was open.

Counted: the shopper speaking plus a 300 ms hangover, the wait until the
first useful reply, assistant audio until a barge-in, and up to five seconds
of listening grace. Not counted: a hidden tab, a muted microphone, a closed
socket, and any gap after the grace.
"""

from __future__ import annotations

from dataclasses import dataclass, field


HANGOVER_S = 0.3
GRACE_S = 5.0


@dataclass
class Interval:
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ActiveClock:
    speech: list[Interval] = field(default_factory=list)
    waiting: list[Interval] = field(default_factory=list)
    assistant: list[Interval] = field(default_factory=list)
    grace: list[Interval] = field(default_factory=list)
    hidden: bool = False
    muted: bool = False
    closed: bool = False

    def shopper_spoke(self, start: float, end: float) -> None:
        if self.hidden or self.muted or self.closed:
            return
        self.speech.append(Interval(start, end + HANGOVER_S))

    def waited(self, start: float, first_audio: float) -> None:
        if self.closed:
            return
        self.waiting.append(Interval(start, first_audio))

    def assistant_spoke(
        self, start: float, end: float, *, barge_at: float | None = None
    ) -> None:
        if self.closed:
            return
        stop = end if barge_at is None else min(end, barge_at)
        self.assistant.append(Interval(start, stop))

    def listening_grace(self, start: float, end: float) -> None:
        if self.hidden or self.muted or self.closed:
            return
        self.grace.append(Interval(start, min(end, start + GRACE_S)))

    def active_seconds(self) -> float:
        return sum(
            item.seconds
            for item in (*self.speech, *self.waiting, *self.assistant, *self.grace)
        )

    def active_minutes(self) -> float:
        return self.active_seconds() / 60.0


def rupees_per_active_minute(total_inr: float, active_seconds: float) -> float:
    if active_seconds <= 0:
        return 0.0
    return total_inr / active_seconds * 60.0
