"""Limits that end a session before it spends more than it should.

A Google Cloud budget is an alert, not a cap. It emails when the threshold is
crossed and the API keeps billing, so a session nobody is watching - a tab left
open, a reconnect loop, a caller who walks away - runs until something else
stops it. Nothing else was stopping it.

Four independent ceilings, because they fail in different ways:

  turns        a loop that keeps asking and answering
  minutes      a session held open with nobody on it
  voice/min    the commercial ceiling: what the two audio streams cost
  rupees       anything the first three did not anticipate

The voice rate is the one that matters commercially. Poorvika's ceiling is
Rs 2 a connected minute for the caller's audio in and the agent's audio out
together, and that is what this measures - not the accumulated text context,
whose billing is still an open question.

That distinction is not academic. The absolute ceiling, computed on the
pessimistic reading of that open question, ended a perfectly ordinary
three-minute call at Rs 20.96 - of which Rs 17.97 was an assumption and only
Rs 2.99 was anything the agent had actually said. So the absolute figure is
now a true backstop, set where it will not fire on a normal call, and the
commercial limit is enforced on the number it is actually about.
"""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    """A ceiling was reached. Carries something that can be said aloud."""

    def __init__(self, reason: str, spoken: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.spoken = spoken


@dataclass(frozen=True)
class SessionBudget:
    max_turns: int = 40
    max_minutes: float = 10.0
    max_inr: float = 50.0
    # Rs 2 a connected minute for voice in plus voice out, inclusive of tax.
    max_audio_inr_per_minute: float = 2.0
    # A rate measured over seconds is noise. The greeting alone is a whole
    # reply inside the first ten seconds of a call, and dividing it by a
    # fraction of a minute reads as a rate no real call would ever sustain.
    rate_check_after_minutes: float = 1.0

    def check(
        self,
        turns: int,
        minutes: float,
        inr: float,
        audio_inr_per_minute: float = 0.0,
    ) -> None:
        """Raise if any ceiling is reached. Called after each completed turn."""
        if turns >= self.max_turns:
            raise BudgetExceeded(
                f"turn ceiling reached ({turns} >= {self.max_turns})",
                "We have covered a lot here. Let me pass you to someone who can help further.",
            )
        if minutes >= self.max_minutes:
            raise BudgetExceeded(
                f"time ceiling reached ({minutes:.1f} >= {self.max_minutes} min)",
                "We have been talking a while. Let me pass you to someone who can help further.",
            )
        if (
            minutes >= self.rate_check_after_minutes
            and audio_inr_per_minute >= self.max_audio_inr_per_minute
        ):
            raise BudgetExceeded(
                f"voice ceiling reached (Rs {audio_inr_per_minute:.2f}/min >= "
                f"Rs {self.max_audio_inr_per_minute}/min)",
                "I need to hand you over now. Someone will be with you shortly.",
            )
        if inr >= self.max_inr:
            # The backstop. If this is what stops a session, the other two
            # ceilings were set wrong and the numbers want revisiting.
            raise BudgetExceeded(
                f"spend ceiling reached (Rs {inr:.2f} >= Rs {self.max_inr})",
                "I need to hand you over now. Someone will be with you shortly.",
            )
