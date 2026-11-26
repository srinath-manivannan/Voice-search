"""Shared Guided state: expiry, the rank reservation, and one shelf-warm leader.

The live ``Session`` object stays in the process that is talking to the
shopper. What has to be the same on every worker is the clock, the one
₹0.25 ranking charge, the rate limit, and which process is allowed to warm
the shelves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

IDLE_SECONDS = 15 * 60
ABSOLUTE_SECONDS = 45 * 60
RANK_PAISE = 25  # ₹0.25 including GST
WARM_LOCK_MS = 600_000


@dataclass
class RankHold:
    ok: bool
    reason: str = ""


class MemoryDirectory:
    """One process. The same rules Redis applies, without a network."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[float, float]] = {}
        self._ranks: dict[str, str] = {}
        self._warm_until = 0.0
        self._rates: dict[str, list[float]] = {}

    def touch(self, sid: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        created, updated = self._seen.get(sid, (moment, moment))
        if moment - created > ABSOLUTE_SECONDS or moment - updated > IDLE_SECONDS:
            self._seen.pop(sid, None)
            return False
        self._seen[sid] = (created, moment)
        return True

    def open(self, sid: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        self._seen[sid] = (moment, moment)

    def reserve_rank(self, sid: str) -> RankHold:
        rid = f"guided:{sid}:rank"
        state = self._ranks.get(rid)
        if state == "held":
            return RankHold(True, "replay")
        if state == "settled":
            return RankHold(False, "settled")
        if state == "released":
            return RankHold(False, "released")
        self._ranks[rid] = "held"
        return RankHold(True, "ok")

    def settle_rank(self, sid: str) -> None:
        rid = f"guided:{sid}:rank"
        if self._ranks.get(rid) == "held":
            self._ranks[rid] = "settled"

    def release_rank(self, sid: str) -> None:
        rid = f"guided:{sid}:rank"
        if self._ranks.get(rid) == "held":
            self._ranks[rid] = "released"

    def claim_warm(self, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        if moment < self._warm_until:
            return False
        self._warm_until = moment + WARM_LOCK_MS / 1000
        return True

    def allow(self, client: str, limit: int, *, now: float | None = None) -> bool:
        if limit <= 0:
            return True
        moment = time.monotonic() if now is None else now
        starts = [item for item in self._rates.get(client, []) if moment - item < 60]
        if len(starts) >= limit:
            self._rates[client] = starts
            return False
        starts.append(moment)
        self._rates[client] = starts
        return True
