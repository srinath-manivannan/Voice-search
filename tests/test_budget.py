"""Session ceilings.

A Google Cloud budget is an alert: it emails at the threshold and the API
keeps billing. With a $5 limit set and no cap in the code, a tab left open or
a reconnect loop had nothing stopping it. These are the thing that stops it.
"""

from __future__ import annotations

import pytest

from src.voice.budget import BudgetExceeded, SessionBudget


def test_an_ordinary_call_passes_every_ceiling():
    SessionBudget().check(turns=6, minutes=3.0, inr=2.40)


def test_a_loop_that_keeps_answering_is_stopped_by_turns():
    with pytest.raises(BudgetExceeded, match="turn ceiling"):
        SessionBudget(max_turns=40).check(turns=40, minutes=1.0, inr=0.5)


def test_a_session_nobody_is_on_is_stopped_by_time():
    with pytest.raises(BudgetExceeded, match="time ceiling"):
        SessionBudget(max_minutes=10).check(turns=2, minutes=10.1, inr=0.5)


def test_spend_is_the_backstop_for_whatever_the_others_missed():
    with pytest.raises(BudgetExceeded, match="spend ceiling"):
        SessionBudget(max_inr=20).check(turns=3, minutes=2.0, inr=20.0)


def test_every_ceiling_carries_something_that_can_be_said_to_a_caller():
    """A caller hears a sentence, not a reason code."""
    for budget, args in (
        (SessionBudget(max_turns=1), dict(turns=1, minutes=0.0, inr=0.0)),
        (SessionBudget(max_minutes=1), dict(turns=0, minutes=1.0, inr=0.0)),
        (SessionBudget(max_inr=1), dict(turns=0, minutes=0.0, inr=1.0)),
    ):
        with pytest.raises(BudgetExceeded) as stop:
            budget.check(**args)
        spoken = stop.value.spoken
        assert spoken.endswith(".") and len(spoken.split()) >= 5
        # The reason is for the log; it must not be what the caller hears.
        assert "ceiling" not in spoken.lower()


def test_the_ceiling_is_inclusive_so_the_limit_is_not_exceeded_first():
    """At the limit, not one turn past it."""
    with pytest.raises(BudgetExceeded):
        SessionBudget(max_turns=5).check(turns=5, minutes=0.0, inr=0.0)
    SessionBudget(max_turns=5).check(turns=4, minutes=0.0, inr=0.0)
