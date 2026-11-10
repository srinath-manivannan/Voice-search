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


def test_the_voice_floor_counts_the_callers_voice_once_not_once_per_turn():
    """`prompt_audio` is cumulative, so the last turn already holds all of it.

    This is the FLOOR, reported beside the bill: the irreducible part, the
    microphone and the mouth, once each. It is not the charge - Google
    re-processes the window every turn and `total_usd` is what that costs -
    and keeping the two apart is the only way to see how much of a call is
    physics and how much is a setting.
    """
    from src.voice.usage import SessionUsage, TurnUsage

    usage = SessionUsage()
    # One caller, speaking more as the call goes on; the agent replying.
    for heard, spoken in ((500, 100), (1200, 100), (1800, 100)):
        usage.turns.append(TurnUsage(prompt_audio=heard, response_audio=spoken))

    # 1800 heard once at $3/M, 300 spoken at $12/M.
    expected = (1800 * 3.00 + 300 * 12.00) / 1e6
    assert usage.voice_usd == pytest.approx(expected)
    # The actual charge is higher, because every turn re-bills the window.
    assert usage.total_usd > usage.voice_usd


def test_the_rate_ceiling_is_the_whole_bill_not_just_the_voice():
    """Rs 2.50 a connected minute, every charge line, GST included.

    The old ceiling watched the two audio streams only. On the measured call
    they were Rs 1.39 of Rs 9.01 a minute, so that ceiling would have waved
    through a call costing six times its limit.
    """
    budget = SessionBudget()
    # Comfortably inside: nothing raised.
    budget.check(turns=4, minutes=3.0, inr=7.0, inr_per_minute=2.36)

    with pytest.raises(BudgetExceeded, match="rate ceiling"):
        budget.check(turns=4, minutes=3.0, inr=9.0, inr_per_minute=2.6)


def test_a_rate_measured_over_seconds_does_not_end_the_call():
    """The greeting is a whole reply inside the first ten seconds.

    Divided by a fraction of a minute that reads as a rate no real call would
    ever sustain, and cutting a caller off during the greeting would be the
    worst possible first impression.
    """
    SessionBudget().check(turns=1, minutes=0.15, inr=0.2, inr_per_minute=11.0)


def test_the_absolute_ceiling_no_longer_ends_an_ordinary_call():
    """It fired at Rs 20.96 three minutes into a normal conversation.

    That figure turned out to be roughly right - the docs confirm the context
    is re-billed - but a backstop is not the thing that should be enforcing
    the commercial limit. The rate ceiling does that now, and this one exists
    for whatever the rate ceiling does not foresee.
    """
    SessionBudget().check(turns=18, minutes=3.0, inr=20.96, inr_per_minute=2.3)
