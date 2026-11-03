"""Token accounting and cost for a Live API session.

Two things measured in Phase 0 make this file necessary rather than a one-line
multiplication.

First, `thoughtsTokenCount` is charged but is NOT included in `totalTokenCount`.
A meter built on totalTokenCount under-reports every call. Thinking averaged 126
tokens a turn against 205 response tokens - not a rounding error.

Second, it is not yet established whether the accumulated prompt is re-billed on
every turn. `promptTokenCount` is reported cumulatively, which is consistent with
both billing models, and they differ by about 3.3x over twenty turns. Until the
billing console settles it, this reports BOTH and the caller decides which to
trust. Reporting a single confident number here would be inventing one.

Rates: https://aistudio.google.com/docs/pricing, read 2026-11-03.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per 1M tokens.
PROMPT_AUDIO = 3.00
PROMPT_TEXT = 0.75
RESPONSE_AUDIO = 12.00
RESPONSE_TEXT = 4.50
# Thinking is billed at the text output rate.
THINKING = 4.50


def _by_modality(details: list[dict] | None) -> dict[str, int]:
    return {
        d.get("modality", "UNKNOWN"): d.get("tokenCount", 0) for d in (details or [])
    }


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token counts, as the server reported them.

    `prompt_*` are cumulative for the session - that is how the API reports them,
    and flattening it here would lose the information the two cost hypotheses
    need.
    """

    prompt_audio: int = 0
    prompt_text: int = 0
    response_audio: int = 0
    response_text: int = 0
    thoughts: int = 0

    @classmethod
    def parse(cls, usage_metadata: dict) -> TurnUsage:
        p = _by_modality(usage_metadata.get("promptTokensDetails"))
        r = _by_modality(usage_metadata.get("responseTokensDetails"))
        return cls(
            prompt_audio=p.get("AUDIO", 0),
            prompt_text=p.get("TEXT", 0),
            response_audio=r.get("AUDIO", 0),
            response_text=r.get("TEXT", 0),
            thoughts=usage_metadata.get("thoughtsTokenCount") or 0,
        )

    @property
    def prompt_total(self) -> int:
        return self.prompt_audio + self.prompt_text

    @property
    def billable_output(self) -> int:
        """Output tokens actually charged, thinking included.

        This is the number `totalTokenCount` leaves out.
        """
        return self.response_audio + self.response_text + self.thoughts

    def output_usd(self) -> float:
        return (
            self.response_audio * RESPONSE_AUDIO
            + self.response_text * RESPONSE_TEXT
            + self.thoughts * THINKING
        ) / 1e6

    def prompt_usd(self) -> float:
        return (self.prompt_audio * PROMPT_AUDIO + self.prompt_text * PROMPT_TEXT) / 1e6


@dataclass
class SessionUsage:
    """Accumulates turns and prices the session under both billing hypotheses."""

    turns: list[TurnUsage] = field(default_factory=list)

    def record(self, usage_metadata: dict) -> TurnUsage:
        turn = TurnUsage.parse(usage_metadata)
        self.turns.append(turn)
        return turn

    @property
    def output_usd(self) -> float:
        """Identical under both hypotheses - each turn's output is charged once."""
        return sum(t.output_usd() for t in self.turns)

    @property
    def input_usd_rebilled(self) -> float:
        """H1: every turn is charged its full accumulated prompt."""
        return sum(t.prompt_usd() for t in self.turns)

    @property
    def input_usd_billed_once(self) -> float:
        """H2: context is cached, so only the final prompt is charged."""
        return self.turns[-1].prompt_usd() if self.turns else 0.0

    def totals_usd(self) -> dict[str, float]:
        return {
            "output": self.output_usd,
            "rebilled": self.input_usd_rebilled + self.output_usd,
            "billed_once": self.input_usd_billed_once + self.output_usd,
        }

    def report_inr(
        self, usd_inr: float, gst_rate: float, minutes: float
    ) -> dict[str, float]:
        """Per-connected-minute cost in rupees, inclusive of GST.

        `minutes` is wall-clock connected time, not speaking time - the ceiling
        we are held to is per connected minute.
        """
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        t = self.totals_usd()
        gross = 1.0 + gst_rate
        return {
            "rebilled_inr_per_min": t["rebilled"] * usd_inr * gross / minutes,
            "billed_once_inr_per_min": t["billed_once"] * usd_inr * gross / minutes,
            "turns": float(len(self.turns)),
        }
