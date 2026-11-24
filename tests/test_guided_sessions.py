"""The governor is arithmetic, so it is tested as arithmetic.

Rs 1.50 a connected minute is the target and Rs 1.70 is where everything
paid stops. Rs 89 to the dollar and 18% GST throughout, so one dollar is
Rs 105.02 and the numbers below can be checked on paper.
"""

from __future__ import annotations

import pytest

from src.guided.sessions import Governor, Sessions, UnknownSession
from src.guided.thinker import WORST_CASE_USD


def governed(now: list[float], **kwargs) -> tuple[Governor, Sessions]:
    """The arithmetic below is worked at the owner's stated lines - Rs 1.50
    target, Rs 1.70 ceiling - with the per-session cap out of the way, so
    each test checks one rule. The shipped defaults are stricter still and
    are pinned by their own test."""
    clock = lambda: now[0]  # noqa: E731
    lines = {
        "target_inr_per_minute": 1.50,
        "ceiling_inr_per_minute": 1.70,
        "session_cap_inr": 3.00,
        **kwargs,
    }
    return Governor(usd_inr=89.0, gst_rate=0.18, clock=clock, **lines), Sessions(
        clock=clock
    )


def on_the_line(now: list[float], sessions: Sessions, session, until: float) -> None:
    """A customer answering every ten seconds until `until`. Only time like
    this is earned: the governor counts seconds the session was touched."""
    while now[0] < until:
        now[0] = min(until, now[0] + 10.0)
        sessions.get(session.id)


def test_the_shipped_defaults_are_stricter_than_the_owners_lines():
    """Under Rs 1 a minute, and never more than 25 paise on models in one
    call however long it runs."""
    governor = Governor(usd_inr=89.0, gst_rate=0.18)
    assert governor.target_inr_per_minute <= 1.50
    assert governor.ceiling_inr_per_minute <= 1.70
    assert governor.session_cap_inr == 0.25


def test_the_session_cap_holds_however_long_the_call():
    now = [0.0]
    governor, sessions = governed(now, session_cap_inr=0.25)
    session = sessions.open()
    for second in range(0, 3_600, 20):  # an hour on the line
        now[0] = float(second)
        sessions.get(session.id)

    session.spent_usd = 0.20 / 105.02
    assert governor.allows(session, 0.05 / 105.02)
    assert not governor.allows(session, 0.06 / 105.02), "25 paise, not a paisa more"


def test_a_think_has_to_be_earned_by_time_on_the_line():
    """The worst a think can cost is about 21 paise: 8.4 seconds at Rs 1.50."""
    now = [0.0]
    governor, sessions = governed(now)
    session = sessions.open()

    assert governor.inr(WORST_CASE_USD) == pytest.approx(0.21, abs=0.01)
    on_the_line(now, sessions, session, 2.0)
    assert not governor.allows(session, WORST_CASE_USD), "a script two seconds in"
    on_the_line(now, sessions, session, 9.0)
    assert governor.allows(session, WORST_CASE_USD), "a customer who answered"


def test_admission_is_against_the_most_a_call_can_cost_so_the_target_holds_after_it():
    now = [60.0]
    governor, sessions = governed(now)
    session = sessions.open()
    on_the_line(now, sessions, session, 120.0)  # one minute: Rs 1.50 earned

    session.spent_usd = 1.20 / 105.02
    assert governor.allows(session, 0.29 / 105.02)
    assert not governor.allows(session, 0.31 / 105.02), (
        "would end over Rs 1.50 a minute"
    )


def test_at_the_ceiling_nothing_paid_is_allowed_however_small():
    now = [0.0]
    governor, sessions = governed(now)
    session = sessions.open()
    on_the_line(now, sessions, session, 60.0)

    session.spent_usd = 1.70 / 105.02
    assert governor.rate(session) == pytest.approx(1.70, abs=0.001)
    assert not governor.allows(session, 0.0)

    on_the_line(now, sessions, session, 120.0)  # time brings the rate to 0.85
    assert governor.allows(session, 0.001)


def test_a_target_set_above_the_ceiling_by_mistake_does_not_lift_the_ceiling():
    """The one way admission alone would let a session past Rs 1.70.

    With the target under the ceiling, admission keeps every session under
    both. Somebody typing GUIDED_TARGET_INR_PER_MINUTE=2.50 into a deploy
    file must not be how the dead limit stops being one.
    """
    now = [0.0]
    governor, sessions = governed(now, target_inr_per_minute=2.50)
    session = sessions.open()
    on_the_line(now, sessions, session, 60.0)

    session.spent_usd = 1.75 / 105.02
    assert not governor.allows(session, 0.05 / 105.02), (
        "Rs 2.50 was 'earned'; 1.70 is the limit"
    )
    session.spent_usd = 1.00 / 105.02
    assert governor.allows(session, 0.05 / 105.02)


def test_a_page_left_open_all_afternoon_does_not_earn_all_afternoon():
    now = [0.0]
    governor, sessions = governed(now, session_cap_inr=3.0)
    session = sessions.open()
    now[0] = 4 * 3600.0

    session.spent_usd = 2.90 / 105.02
    assert not governor.allows(session, 0.20 / 105.02), (
        "four untouched hours earn nothing, and Rs 3 is the cap besides"
    )


def test_the_meter_reports_what_was_counted_and_the_two_lines_it_is_held_to():
    now = [0.0]
    governor, sessions = governed(now)
    session = sessions.open()
    session.spent_usd, session.thinks = 0.0007, 1
    on_the_line(now, sessions, session, 30.0)

    meter = governor.meter(session)

    assert meter["total_inr"] == pytest.approx(0.074, abs=0.001)
    assert meter["total_inr_per_min"] == pytest.approx(0.147, abs=0.001)
    assert (meter["target_inr_per_min"], meter["ceiling_inr_per_min"]) == (1.50, 1.70)


def test_a_rate_over_no_time_at_all_is_still_a_number():
    now = [5.0]
    governor, sessions = governed(now)

    assert governor.meter(sessions.open())["total_inr_per_min"] == 0.0


def test_a_session_is_forgotten_after_a_quarter_of_an_hour_idle():
    now = [0.0]
    _, sessions = governed(now)
    session = sessions.open()

    now[0] = 800.0
    assert sessions.get(session.id) is session, "touched, so the clock restarts"
    now[0] = 1_600.0
    assert sessions.get(session.id) is session
    now[0] = 2_600.0
    with pytest.raises(UnknownSession):
        sessions.get(session.id)


def test_an_id_nobody_was_given_opens_nothing():
    _, sessions = governed([0.0])

    with pytest.raises(UnknownSession):
        sessions.get("AAAAAAAAAAAAAAAAAAAAAA")


def test_opening_sessions_cannot_fill_the_process():
    now = [0.0]
    sessions = Sessions(most=3, clock=lambda: now[0])

    ids = [sessions.open().id for _ in range(10)]

    assert len(sessions) == 3
    assert sessions.get(ids[-1]).id == ids[-1], (
        "the newest was dropped instead of the oldest"
    )
    assert len({*ids}) == 10 and all(len(i) >= 16 for i in ids)
