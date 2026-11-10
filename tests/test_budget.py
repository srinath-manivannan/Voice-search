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


def test_the_caller_s_voice_is_counted_once_not_once_per_turn():
    """`prompt_audio` is cumulative, so the last turn already holds all of it.

    Summing it would charge the first minute of a call again on every turn
    after it - which is the open billing question, not the floor. The floor is
    what the microphone physically sent, once.
    """
    from src.voice.usage import SessionUsage, TurnUsage

    usage = SessionUsage()
    # One caller, speaking more as the call goes on; the agent replying.
    for heard, spoken in ((500, 100), (1200, 100), (1800, 100)):
        usage.turns.append(TurnUsage(prompt_audio=heard, response_audio=spoken))

    # 1800 heard once at $3/M, 300 spoken at $12/M.
    expected = (1800 * 3.00 + 300 * 12.00) / 1e6
    assert usage.audio_usd == pytest.approx(expected)
    # And not the sum of the cumulative figures, which would be 3500 heard.
    assert usage.audio_usd < (3500 * 3.00 + 300 * 12.00) / 1e6


def test_the_voice_ceiling_is_what_poorvika_is_held_to():
    """Rs 2 a connected minute, voice in plus voice out, tax included."""
    from src.voice.budget import BudgetExceeded, SessionBudget

    budget = SessionBudget()
    # Comfortably inside: nothing raised.
    budget.check(turns=4, minutes=3.0, inr=5.0, audio_inr_per_minute=1.37)

    with pytest.raises(BudgetExceeded, match="voice ceiling"):
        budget.check(turns=4, minutes=3.0, inr=5.0, audio_inr_per_minute=2.4)


def test_a_rate_measured_over_seconds_does_not_end_the_call():
    """The greeting is a whole reply inside the first ten seconds.

    Divided by a fraction of a minute that reads as a rate no real call would
    ever sustain, and cutting a caller off during the greeting would be the
    worst possible first impression.
    """
    from src.voice.budget import SessionBudget

    SessionBudget().check(turns=1, minutes=0.15, inr=0.2, audio_inr_per_minute=11.0)


def test_the_absolute_ceiling_no_longer_ends_an_ordinary_call():
    """It fired at Rs 20.96 three minutes into a normal conversation.

    Rs 17.97 of that was an assumption about context billing and only Rs 2.99
    was anything the agent had said. A backstop that fires on ordinary traffic
    is not a backstop.
    """
    from src.voice.budget import SessionBudget

    SessionBudget().check(turns=18, minutes=3.0, inr=20.96, audio_inr_per_minute=1.37)
