"""Who is mid-way through, what they have spent, and what they may spend.

## The governor

The brief is Rs 1.50 a connected minute, and Rs 1.70 with nothing allowed
past it. Two rules hold that, and neither is a hope:

- **Admission.** A paid call is allowed only if, having made it, the session
  would still be at or under the target: `spent + this call <= 1.50 x
  minutes connected`. "This call" is the most the call CAN cost, not what it
  usually does, so the sum is a bound rather than an estimate.
- **The ceiling.** A session at or over 1.70 a minute is refused everything
  paid until time brings it back under. With the target below the ceiling,
  admission alone already keeps a session under both, and this rule decides
  nothing. It is there for the day the target is configured above it: the
  target is a setting, and the dead limit must not move because a setting
  did.

The questions are free, so the allowance builds while they are answered. A
real customer has thirty seconds behind them by the time anything is bought
and has earned it several times over; a script that asks for a think one
second after starting has earned nothing and gets the unpaid answer.

A flat cap per session sits on top, because "per minute" on a web page that
is left open all afternoon is an allowance that only grows.

## The sessions

In memory, per process, forgotten after a quarter of an hour idle. That is
right for one container and wrong for several, and the day there are several
this is a shared store rather than a cleverer dict.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.guided.answers import ProductFacts
from src.guided.engine import Answers
from src.guided.recommend import Pick

IDLE_SECONDS = 900.0
# The most sessions held at once. Each is a few hundred bytes; this is the
# line that stops "open a session" being a way to fill the process.
MOST_SESSIONS = 5_000


class UnknownSession(KeyError):
    """No such session - never opened, or idle long enough to be forgotten."""


@dataclass
class Session:
    id: str
    started: float
    touched: float
    # None until the first screen is answered; every fixed line after that
    # is said in this language.
    verified: bool = False
    rank_attempted: bool = False
    reserved_usd: float = 0.0
    active_seconds: float = 0.0
    edit_field: bool = False
    latest_card: dict[str, Any] = field(default_factory=dict)
    language: str | None = None
    shelf: str | None = None
    answers: Answers = ()
    shown: tuple[Pick, ...] = ()
    # The whole ranked order behind the suggestion, so "show me another"
    # walks it one at a time and "show the list" lays it out.
    ranked: tuple[Pick, ...] = ()
    at: int = 0
    # Refs turned down with "இதுல எதுவும் வேண்டாம், வேற காட்டு" - never
    # offered again in this ranking. Cleared when a new ranking is made.
    rejected: tuple[str, ...] = ()
    # A said brand with nothing in the said budget: the choice put to them -
    # another brand at this budget, or this brand from its next price.
    clash: dict[str, Any] | None = None
    picked: str | None = None
    # The page of facts about the picked product, being fetched or fetched.
    # A task rather than a value so that picking does not wait for it and
    # the first question does, exactly as long as it has to.
    facts: asyncio.Task[ProductFacts] | None = None
    # How many of their own questions have been asked about this pick.
    asked: int = 0
    spent_usd: float = 0.0
    thinks: int = 0
    # How many times the customer has spoken. Capped: see `hear.MOST_TURNS`.
    heard: int = 0
    # Sentences the browser heard for free and the code matched. Counted so
    # the meter can show speech happening at ₹0, not just assert it.
    said: int = 0
    # Sentences typed into the Guided box: the same free path, counted apart.
    typed: int = 0
    # Answers said before their question came up - "engineering" said while
    # the brand was being asked - kept and pressed when it does, the way a
    # salesperson remembers what you told them. (question, choice) pairs.
    pending: tuple[tuple[str, str], ...] = ()
    # The shops holding the picked product, kept from the live stock answer
    # so "talk to your person" can name them without another call.
    branches: tuple[dict[str, Any], ...] = ()
    # Spoken names that matched no button on their question - "Nokia" on a
    # shelf with no Nokia - cleared at each hear, read by the reply.
    missed: tuple[str, ...] = ()


class Sessions:
    def __init__(
        self,
        idle_s: float = IDLE_SECONDS,
        most: int = MOST_SESSIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._idle = idle_s
        self._most = most
        self._clock = clock
        self._open: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._open)

    def open(self) -> Session:
        now = self._clock()
        self._forget(now)
        # 128 bits from the OS. The id is the only thing standing between
        # one customer's session and another's, so it is not a counter.
        session = Session(secrets.token_urlsafe(16), started=now, touched=now)
        self._open[session.id] = session
        return session

    def restore(self, session: Session) -> None:
        self._forget(self._clock())
        self._open[session.id] = session

    def get(self, session_id: str) -> Session:
        now = self._clock()
        session = self._open.get(session_id)
        if session is None or now - session.touched > self._idle:
            self._open.pop(session_id, None)
            raise UnknownSession(session_id)
        session.active_seconds += min(max(0, now - session.touched), 30.0)
        session.touched = now
        return session

    def _forget(self, now: float) -> None:
        stale = [
            key for key, held in self._open.items() if now - held.touched > self._idle
        ]
        for key in stale:
            del self._open[key]
        # Still full of live ones: the longest idle goes. A dict keeps
        # insertion order, and that is close enough to oldest.
        while len(self._open) >= self._most:
            del self._open[next(iter(self._open))]


@dataclass(frozen=True)
class Governor:
    usd_inr: float
    gst_rate: float
    target_inr_per_minute: float = 1.0
    ceiling_inr_per_minute: float = 1.0
    session_cap_inr: float = 0.25
    clock: Callable[[], float] = field(default=time.time, compare=False)

    def inr(self, usd: float) -> float:
        """Dollars as the rupees the retailer is charged, GST in."""
        return usd * self.usd_inr * (1 + self.gst_rate)

    def minutes(self, session: Session) -> float:
        # Never zero: a rate over no time at all is not a number.
        return max(session.active_seconds, 1.0) / 60.0

    def rate(self, session: Session) -> float:
        return self.inr(session.spent_usd) / self.minutes(session)

    def allows(self, session: Session, most_usd: float) -> bool:
        """May this session make a call that costs at most this much?"""
        if self.rate(session) >= self.ceiling_inr_per_minute:
            return False
        after = self.inr(session.spent_usd + session.reserved_usd + most_usd)
        earned = min(
            self.target_inr_per_minute * self.minutes(session), self.session_cap_inr
        )
        return after <= earned

    def meter(self, session: Session) -> dict[str, Any]:
        """What the side box shows. Counted, never smoothed."""
        total = self.inr(session.spent_usd)
        return {
            "total_inr": round(total, 3),
            "total_inr_per_min": round(self.rate(session), 3),
            "target_inr_per_min": self.target_inr_per_minute,
            "ceiling_inr_per_min": self.ceiling_inr_per_minute,
            "seconds": round(self.clock() - session.started, 1),
            "thinks": session.thinks,
            "active_seconds": round(session.active_seconds, 2),
            "reserved_inr": round(self.inr(session.reserved_usd), 4),
            "session_limit_inr": self.session_cap_inr,
            "discovery_external_inr": 0.0,
            "spoken_turns": session.said,
            "typed_turns": session.typed,
        }
