"""Limits that end a session before it spends more than it should.

A Google Cloud budget is an alert, not a cap. It emails when the threshold is
crossed and the API keeps billing, so a session nobody is watching - a tab left
open, a reconnect loop, a caller who walks away - runs until something else
stops it. Nothing else was stopping it.

Three independent ceilings, because they fail in different ways:

  turns     a loop that keeps asking and answering
  minutes   a session held open with nobody on it
  rupees    anything the first two did not anticipate

The money one is the backstop. The other two exist because a caller gets a
clearer ending from "we have been talking a while" than from being cut off
mid-sentence when a number is reached.
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
    max_inr: float = 20.0

    def check(self, turns: int, minutes: float, inr: float) -> None:
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
        if inr >= self.max_inr:
            # The backstop. If this is what stops a session, the other two
            # ceilings were set wrong and the numbers want revisiting.
            raise BudgetExceeded(
                f"spend ceiling reached (Rs {inr:.2f} >= Rs {self.max_inr})",
                "I need to hand you over now. Someone will be with you shortly.",
            )
