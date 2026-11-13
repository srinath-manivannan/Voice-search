"""Token accounting for a Live API session. Counting here, pricing in cost.py.

Two things measured in Phase 0 make this file necessary rather than a one-line
multiplication.

First, `thoughtsTokenCount` is charged but is NOT included in
`totalTokenCount`. A meter built on totalTokenCount under-reports every call.

Second, `prompt_*` are reported CUMULATIVELY for the session. That used to be
read two ways here - billed once, or billed again every turn - and the two
differed by 7x, which made every cost conversation unresolvable. Google's Live
API best-practices page settles it: "Past tokens are re-processed and
accounted for in each new turn." So the sum across turns is the charge, and
the second reading is gone. `src/voice/cost.py` carries the citations.

What survives of the old caution is this: the cumulative figures must be
summed, not averaged or taken from the last turn, and that is easy to get
wrong by looking at one turn and thinking it looks reasonable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.voice.cost import (
    FLASH_LITE,
    LIVE,
    Breakdown,
    Rates,
    grok_rates,
    grok_usd,
    price,
)

__all__ = ["FLASH_LITE", "LIVE", "Rates", "SessionUsage", "TurnUsage"]


def _by_modality(details: list[dict[str, Any]] | None) -> dict[str, int]:
    return {
        d.get("modality", "UNKNOWN"): d.get("tokenCount", 0) for d in (details or [])
    }


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token counts, as the server reported them.

    `prompt_*` are cumulative for the session - that is how the API reports
    them. Flattening it here would lose the fact that each turn is charged for
    the whole window as it stood then, which is the charge.
    """

    prompt_audio: int = 0
    prompt_text: int = 0
    response_audio: int = 0
    response_text: int = 0
    thoughts: int = 0

    @classmethod
    def parse(cls, usage_metadata: dict[str, Any]) -> TurnUsage:
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
    def billable_output(self) -> int:
        """Output tokens actually charged, thinking included.

        This is the number `totalTokenCount` leaves out.
        """
        return self.response_audio + self.response_text + self.thoughts

    @classmethod
    def parse_text(cls, usage_metadata: dict[str, Any]) -> TurnUsage:
        """The text API reports flat counts, not the Live API's modality split."""
        return cls(
            prompt_text=usage_metadata.get("promptTokenCount", 0),
            response_text=usage_metadata.get("candidatesTokenCount", 0),
            thoughts=usage_metadata.get("thoughtsTokenCount") or 0,
        )

    def output_usd(self, rates: Rates = LIVE) -> float:
        return (
            self.response_audio * rates.response_audio
            + self.response_text * rates.response_text
            + self.thoughts * rates.thinking
        ) / 1e6

    def prompt_usd(self, rates: Rates = LIVE) -> float:
        return (
            self.prompt_audio * rates.prompt_audio
            + self.prompt_text * rates.prompt_text
        ) / 1e6


@dataclass
class SessionUsage:
    """Accumulates turns and prices the session."""

    turns: list[TurnUsage] = field(default_factory=list)
    rates: Rates = LIVE
    # Grok is not a Live turn. Dollars are accumulated per completion so two
    # models in one call (a 4.3 summary, then a dearer one) each keep their
    # own card instead of the last rate being applied to the whole pile.
    grok_prompt_tokens: int = 0
    grok_completion_tokens: int = 0
    _grok_usd: float = 0.0

    def record(self, usage_metadata: dict[str, Any]) -> TurnUsage:
        turn = TurnUsage.parse(usage_metadata)
        self.turns.append(turn)
        return turn

    def record_text(self, usage_metadata: dict[str, Any]) -> TurnUsage:
        turn = TurnUsage.parse_text(usage_metadata)
        self.turns.append(turn)
        return turn

    # -- billed token counts -------------------------------------------------
    #
    # Prompt lines are SUMMED across turns because each turn is charged for
    # everything then standing in the context window. Response lines are not,
    # because each reply is generated once.

    @property
    def billed_in_audio(self) -> int:
        return sum(t.prompt_audio for t in self.turns)

    @property
    def billed_in_text(self) -> int:
        return sum(t.prompt_text for t in self.turns)

    @property
    def billed_out_audio(self) -> int:
        return sum(t.response_audio for t in self.turns)

    @property
    def billed_out_text(self) -> int:
        return sum(t.response_text for t in self.turns)

    @property
    def billed_thinking(self) -> int:
        return sum(t.thoughts for t in self.turns)

    @property
    def input_usd(self) -> float:
        """Every turn's full accumulated prompt, which is what is charged."""
        return sum(t.prompt_usd(self.rates) for t in self.turns)

    @property
    def output_usd(self) -> float:
        return sum(t.output_usd(self.rates) for t in self.turns)

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd + self._grok_usd

    @property
    def grok_usd(self) -> float:
        return self._grok_usd

    @property
    def grok_tokens(self) -> int:
        return self.grok_prompt_tokens + self.grok_completion_tokens

    def add_grok(self, prompt: int, cached: int, completion: int, model: str) -> None:
        """Count one Grok completion the call actually paid for.

        An unknown model id is priced on the dearer card. Under-billing a
        summary is the failure that makes the screen look inside the cap
        while the invoice is not.
        """
        self.grok_prompt_tokens += prompt
        self.grok_completion_tokens += completion
        self._grok_usd += grok_usd(prompt, cached, completion, grok_rates(model))

    def breakdown(
        self,
        usd_inr: float,
        gst_rate: float,
        minutes: float,
        *,
        include_grok: bool = False,
    ) -> Breakdown:
        """The charge lines, in rupees a connected minute, GST included.

        `minutes` is wall-clock connected time, not speaking time - the
        ceiling we are held to is per connected minute. The Grok line is
        included when memory is switched on, even before the first summary,
        so the page shows the row the operator was told to look for.
        """
        return price(
            in_audio_tokens=self.billed_in_audio,
            in_text_tokens=self.billed_in_text,
            out_audio_tokens=self.billed_out_audio,
            out_text_tokens=self.billed_out_text,
            thinking_tokens=self.billed_thinking,
            minutes=minutes,
            turns=len(self.turns),
            rates=self.rates,
            usd_inr=usd_inr,
            gst_rate=gst_rate,
            grok_tokens=self.grok_tokens,
            grok_usd=self._grok_usd,
            include_grok=include_grok,
        )

    def inr_per_minute(self, usd_inr: float, gst_rate: float, minutes: float) -> float:
        """The one number the call is held to. Zero before the first turn."""
        if minutes <= 0 or not self.turns:
            return 0.0
        return self.total_usd * usd_inr * (1 + gst_rate) / minutes

    # -- the irreducible part ------------------------------------------------

    @property
    def voice_usd(self) -> float:
        """The two voice streams, each counted once: the floor under the call.

        Not the charge - re-processing is real and is in `total_usd`. This is
        the part no configuration can remove: the caller's microphone and the
        agent's mouth, once each. Reported beside the total so it is visible
        how much of a call is physics and how much is re-processing, because
        only the second kind can be engineered away.

        The caller's side is taken from the LAST turn rather than summed,
        because `prompt_audio` is cumulative and the final turn's figure is
        every second the microphone ever sent, counted once.
        """
        if not self.turns:
            return 0.0
        heard = self.turns[-1].prompt_audio * self.rates.prompt_audio
        spoken = self.billed_out_audio * self.rates.response_audio
        return (heard + spoken) / 1e6

    def voice_inr_per_minute(
        self, usd_inr: float, gst_rate: float, minutes: float
    ) -> float:
        if minutes <= 0:
            return 0.0
        return self.voice_usd * usd_inr * (1 + gst_rate) / minutes
