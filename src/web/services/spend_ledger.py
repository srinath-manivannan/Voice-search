"""One ceiling on everything paid, per day, with no database.

The Live voice, the Chat tab and the guided think each have their own
per-session limits. None of them limits how many sessions there are, and on
a public site that is the number that runs away. This is the one number
above all of them: rupees spent today (GST in), in this process.

Held in memory on purpose - the deployment runs one process and no database
(the owner's plan, 2026-11-25). A restart starts the day's count again,
which can only let a little more through, never block a customer wrongly.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _today() -> dt.date:
    return dt.datetime.now(IST).date()


class DailyLedger:
    def __init__(self, cap_inr: float, today: Callable[[], dt.date] = _today) -> None:
        self._cap = max(0.0, cap_inr)
        self._today = today
        self._day = today()
        self._spent = 0.0

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            log.info("paid spend on %s: ₹%.2f", self._day, self._spent)
            self._day, self._spent = day, 0.0

    @property
    def cap_inr(self) -> float:
        return self._cap

    def spent_inr(self) -> float:
        self._roll()
        return self._spent

    def allows(self, more_inr: float = 0.0) -> bool:
        """May something costing at most this much start now?"""
        self._roll()
        return self._spent + max(0.0, more_inr) <= self._cap and self._spent < self._cap

    def add(self, inr: float) -> None:
        self._roll()
        self._spent += max(0.0, inr)
        if self._spent >= self._cap:
            log.warning(
                "today's paid ceiling is reached: ₹%.2f of ₹%.2f - paid features "
                "pause until midnight, Guided carries on free",
                self._spent,
                self._cap,
            )
