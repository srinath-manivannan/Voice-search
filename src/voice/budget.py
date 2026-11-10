"""Limits that end a session before it spends more than it should.

A Google Cloud budget is an alert, not a cap. It emails when the threshold is
crossed and the API keeps billing, so a session nobody is watching - a tab left
open, a reconnect loop, a caller who walks away - runs until something else
stops it. Nothing else was stopping it.

Four independent ceilings, because they fail in different ways:

  turns        a loop that keeps asking and answering
  minutes      a session held open with nobody on it
  rupees/min   the commercial ceiling: the whole bill, per connected minute
  rupees       anything the first three did not anticipate

The rate ceiling is the one that matters commercially, and it used to measure
the wrong thing. It watched the two audio streams only, on the grounds that
whether Gemini re-bills accumulated context per turn was an open question. It
is not an open question any more - Google's own best-practices page says past
tokens are re-processed and charged on every turn - and on the measured call
the audio streams were Rs 1.39 of Rs 9.01 a minute. A ceiling on that number
would have waved through a call costing six times its limit.

So this now enforces the total: every line of the bill, inclusive of GST, per
connected minute. `cost.py` prices it and explains each line. The absolute
rupee figure stays as a backstop, set where it will not fire on a normal call,
because the thing it exists to catch is whatever these three did not foresee.
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
    # The whole bill per connected minute, inclusive of GST: caller audio and
    # the re-processed history, the standing prompt, the agent's voice,
    # transcription and thinking.
    #
    # This is NOT Poorvika's Rs 2.50 target. It is a runaway guard, and it
    # sits far above the target on purpose. A ceiling set at the commercial
    # target cuts a customer off in the middle of a sentence on a call that
    # is merely more expensive than we would like, which costs the sale and
    # does not refund the rupee. The target belongs on the screen, where
    # someone can act on it; this belongs where a loop gets stopped.
    max_inr_per_minute: float = 8.00
    # A rate measured over seconds is noise. The greeting alone is a whole
    # reply inside the first ten seconds of a call, and dividing it by a
    # fraction of a minute reads as a rate no real call would ever sustain.
    rate_check_after_minutes: float = 1.0

    def check(
        self,
        turns: int,
        minutes: float,
        inr: float,
        inr_per_minute: float = 0.0,
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
            and inr_per_minute >= self.max_inr_per_minute
        ):
            raise BudgetExceeded(
                f"rate ceiling reached (Rs {inr_per_minute:.2f}/min >= "
                f"Rs {self.max_inr_per_minute}/min)",
                "I need to hand you over now. Someone will be with you shortly.",
            )
        if inr >= self.max_inr:
            # The backstop. If this is what stops a session, the other two
            # ceilings were set wrong and the numbers want revisiting.
            raise BudgetExceeded(
                f"spend ceiling reached (Rs {inr:.2f} >= Rs {self.max_inr})",
                "I need to hand you over now. Someone will be with you shortly.",
            )
