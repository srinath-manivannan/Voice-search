"""How many calls one client may start a minute.

This is a cost control before it is an abuse control. A voice session holds a
socket open for minutes and spends real money the whole time, so a page that
reconnects in a loop - a bug, a refresh held down, a script - is a bill, not
just load. `SessionBudget` stops a single call that runs away; nothing stopped
a client opening a hundred of them.

Deliberately in-process and deliberately approximate. A fixed window per
client, counted in memory: it resets on restart and it does not coordinate
across replicas. Both are fine for one container in front of one shop, and
the moment there are several the answer is a shared counter, not a cleverer
version of this.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """A sliding window of recent starts, per client."""

    max_per_minute: int
    window_seconds: float = 60.0
    # Kept small on purpose: one deque per client that has connected
    # recently, pruned on every check, so an idle client costs nothing.
    _seen: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, client: str, now: float | None = None) -> bool:
        """True if this client may start another call right now."""
        if self.max_per_minute <= 0:
            return True
        moment = time.monotonic() if now is None else now
        starts = self._seen.setdefault(client, deque())
        cutoff = moment - self.window_seconds
        while starts and starts[0] < cutoff:
            starts.popleft()
        if len(starts) >= self.max_per_minute:
            return False
        starts.append(moment)
        # An empty deque for a client that has gone quiet is still a dict
        # entry. Drop it, or a long-running server accumulates one per
        # visitor for the life of the process.
        self._seen = {key: value for key, value in self._seen.items() if value}
        return True
