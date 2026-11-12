"""The numbers, in the one format every monitoring system already reads.

Prometheus text, rendered by hand. The client library would be a dependency
for about forty lines of string formatting, and this service has one metrics
endpoint and no exotic metric types.

In-process and per-replica, which is what a scrape expects: the monitoring
system does the aggregating, and a counter that tried to be global would be
a distributed system nobody asked for. It resets on restart, and that is
also what a scrape expects - `rate()` handles it.

**Nothing here identifies anybody.** Counters and buckets only: how many
calls, how long they took, what they cost. No phone number, no transcript,
no item code. A label is a cardinality decision as well as a privacy one -
one label per caller would be both.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

# The buckets are the targets. Phase 1 is held to a p50 under 1 s and a p95
# under 2 s, so the boundaries sit either side of both: a histogram whose
# edges do not bracket the number you promise cannot tell you whether you
# kept the promise.
LATENCY_BUCKETS_MS = (250.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 3000.0, 5000.0)
# Rupees a connected minute. The target is 2.00 and the ceiling that ends a
# call is 8.00; both are boundaries here for the same reason.
RUPEE_BUCKETS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 8.0, 12.0)


@dataclass
class Histogram:
    """Counts per bucket, plus the sum and count Prometheus expects."""

    bounds: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    seen: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.bounds) + 1)

    def observe(self, value: float) -> None:
        if math.isnan(value):
            return
        self.total += value
        self.seen += 1
        for index, bound in enumerate(self.bounds):
            if value <= bound:
                self.counts[index] += 1
                return
        self.counts[-1] += 1

    def quantile(self, fraction: float) -> float:
        """Approximate, from the buckets. Good enough to alert on.

        Returns the upper bound of the bucket the quantile falls in, which
        is the honest answer a histogram can give: "p95 is at or under this".
        """
        if not self.seen:
            return 0.0
        target = fraction * self.seen
        running = 0
        for index, count in enumerate(self.counts):
            running += count
            if running >= target:
                return self.bounds[index] if index < len(self.bounds) else math.inf
        return math.inf


class Metrics:
    """Everything this process is willing to say about itself."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, Histogram] = {}
        self._help: dict[str, str] = {}

    def count(self, name: str, help_text: str, amount: float = 1.0) -> None:
        with self._lock:
            self._help[name] = help_text
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def gauge(self, name: str, help_text: str, value: float) -> None:
        with self._lock:
            self._help[name] = help_text
            self._gauges[name] = value

    def observe(
        self, name: str, help_text: str, value: float, bounds: tuple[float, ...]
    ) -> None:
        with self._lock:
            self._help[name] = help_text
            histogram = self._histograms.setdefault(name, Histogram(bounds))
            histogram.observe(value)

    def histogram(self, name: str) -> Histogram | None:
        return self._histograms.get(name)

    def counter(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def render(self) -> str:
        """Prometheus text exposition, version 0.0.4."""
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self._counters.items()):
                lines += self._head(name, "counter")
                lines.append(f"{name} {_number(value)}")
            for name, value in sorted(self._gauges.items()):
                lines += self._head(name, "gauge")
                lines.append(f"{name} {_number(value)}")
            for name, histogram in sorted(self._histograms.items()):
                lines += self._head(name, "histogram")
                running = 0
                for index, bound in enumerate(histogram.bounds):
                    running += histogram.counts[index]
                    lines.append(f'{name}_bucket{{le="{_number(bound)}"}} {running}')
                lines.append(f'{name}_bucket{{le="+Inf"}} {histogram.seen}')
                lines.append(f"{name}_sum {_number(histogram.total)}")
                lines.append(f"{name}_count {histogram.seen}")
        return "\n".join(lines) + "\n"

    def _head(self, name: str, kind: str) -> list[str]:
        return [f"# HELP {name} {self._help.get(name, '')}", f"# TYPE {name} {kind}"]


def _number(value: float) -> str:
    """Prometheus wants a plain number, not Python's repr of a float."""
    if value == int(value):
        return str(int(value))
    return repr(round(value, 4))


# One per process, like the log. Passed where it can be, reached for where
# a call site would otherwise have to thread it through four layers.
METRICS = Metrics()
