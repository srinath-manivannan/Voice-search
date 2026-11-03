"""A timestamped record of what one call actually did.

Written because of how the bugs in this service were found. The greeting
cutting off took four attempts, the silence took two, and each wrong attempt
was a round trip through a person: describe the symptom, guess a cause, ship
it, wait, hear that it still happens. The one bug fixed first time was the one
where the code was read instead of the symptom reasoned about.

What was missing every time was the same thing - any idea what the call did.
A transcript says what was said; it says nothing about whether the gate was
shut, which turn the audio belonged to, or whether a flush was ours or the
model's. So this records the decisions, not the content.

Deliberately not a logger. Logs are a stream nobody reads; this is one object
per call that can be shown on screen while it happens and handed over whole
afterwards. It holds no audio and no customer speech - only what the service
decided and when.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    at_ms: int
    kind: str
    detail: dict[str, Any]


@dataclass
class CallTrace:
    """Events for one call, in order, timed from the moment it opened."""

    started_at: float = field(default_factory=time.monotonic)
    events: list[TraceEvent] = field(default_factory=list)
    # Bounded: a long call must not grow this without limit. Ten thousand
    # events is hours of conversation and a few hundred KB.
    limit: int = 10_000

    def record(self, kind: str, **detail: Any) -> TraceEvent:
        event = TraceEvent(
            at_ms=int((time.monotonic() - self.started_at) * 1000),
            kind=kind,
            detail=detail,
        )
        if len(self.events) < self.limit:
            self.events.append(event)
        return event

    def of_kind(self, kind: str) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == kind]

    def summary(self) -> dict[str, Any]:
        """The questions actually asked when something goes wrong."""
        turns = self.of_kind("turn_complete")
        latencies = sorted(
            e.detail["first_audio_ms"] for e in turns if e.detail.get("first_audio_ms")
        )
        return {
            "duration_ms": int((time.monotonic() - self.started_at) * 1000),
            "turns": len(turns),
            # Was the agent cut off, and by whom - us or the model?
            "barge_ins_local": len(self.of_kind("barge_in_local")),
            "interrupts_from_model": len(self.of_kind("interrupted_by_model")),
            # Did audio reach the browser at all?
            "audio_chunks": len(self.of_kind("audio_out")),
            "audio_dropped_abandoned": len(self.of_kind("audio_dropped")),
            # Was the caller's turn ever signalled? Its absence was a real bug.
            "turns_signalled": len(self.of_kind("end_of_speech")),
            "frames_held_by_gate": len(self.of_kind("gate_held")),
            "p50_first_audio_ms": latencies[len(latencies) // 2] if latencies else None,
            # round, not int: truncating put p95 of three samples on the
            # middle one, which is the opposite of what a p95 is for.
            "p95_first_audio_ms": (
                latencies[min(round(0.95 * (len(latencies) - 1)), len(latencies) - 1)]
                if latencies
                else None
            ),
        }

    def save(self, directory: Path) -> Path:
        """Write the whole trace where it can be picked up and sent on."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"call-{int(time.time())}.json"
        path.write_text(
            json.dumps(
                {"summary": self.summary(), "events": [asdict(e) for e in self.events]},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path
