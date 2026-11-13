"""What is worth waking somebody for, and at what number.

The thresholds live here rather than in a dashboard for one reason: the
alert that wakes you and the rule that tells you to roll back have to be the
same number, or at three in the morning the argument is about which one is
right instead of about the call that is failing. `docs/ROLLBACK.md` has the
decision table; these are the same figures, and a test holds them together.

Two ways they fire, because the host is not chosen yet (gap 9 in RULES.md):

- **From logs, today.** Every call is evaluated when it ends and anything
  breaching is written as a single `ALERT` line. Any log search can match
  that, which is alerting that works before anybody has picked a monitoring
  product.
- **From metrics, later.** `rules()` renders the same thresholds as
  Prometheus alerting rules, generated from these definitions so the file
  that fires cannot drift from the file that is reviewed.

What is deliberately NOT alerted on: being over the Rs 2.50 target. Every
call is, today. An alert that fires on every call is one people turn off,
and then the real ones go with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from src.observability.metrics import METRICS, Metrics

log = logging.getLogger(__name__)

CRITICAL = "critical"
WARNING = "warning"


@dataclass(frozen=True)
class Rule:
    """One thing worth noticing, and why it is worth it.

    `why` is not decoration. An alert whose reason nobody can state is an
    alert that gets silenced the second time it fires.
    """

    name: str
    severity: str
    why: str
    # Evaluated in-process at the end of a call, against this process's own
    # metrics. Returns the offending value, or None when all is well.
    check: Callable[[Metrics], float | None]
    # The same condition for a scrape-based monitor. PromQL.
    expression: str
    #: How long it must hold before anyone is woken.
    sustained: str = "5m"


def _p95_first_audio(metrics: Metrics) -> float | None:
    """Phase 1 is held to p50 under 1 s and p95 under 2 s."""
    histogram = metrics.histogram("voice_first_audio_ms")
    if histogram is None or histogram.seen < 5:
        return None  # too few to mean anything
    p95 = histogram.quantile(0.95)
    return p95 if p95 > 2000 else None


def _cut_off_callers(metrics: Metrics) -> float | None:
    """A session ceiling ended a call, so somebody was hung up on mid-call.

    The rupee ceiling is a runaway guard set far above the target precisely
    so this does not happen. If it fires, either a call really did run away
    or the ceiling is wrong - and both are worth waking somebody for.
    """
    stopped = metrics.counter("voice_calls_stopped_by_budget_total")
    return stopped if stopped > 0 else None


def _failure_rate(metrics: Metrics) -> float | None:
    """One call in twenty, the figure the rollback runbook uses."""
    calls = metrics.counter("voice_calls_total")
    failed = metrics.counter("voice_calls_failed_total")
    if calls < 20:
        return None
    rate = failed / calls
    return rate if rate > 0.05 else None


def _runaway_cost(metrics: Metrics) -> float | None:
    """Over the ceiling that ends calls, not over the target.

    Every call is over the Rs 2.50 target today. An alert that fires every
    time is an alert somebody turns off.
    """
    histogram = metrics.histogram("voice_rupees_per_minute")
    if histogram is None or histogram.seen < 3:
        return None
    p95 = histogram.quantile(0.95)
    return p95 if p95 >= 8.0 else None


def _upstream(metrics: Metrics) -> float | None:
    """Poorvika's own services. Not our outage, and our problem."""
    failures = metrics.counter("tool_failures_total")
    calls = metrics.counter("tool_calls_total")
    if calls < 20:
        return None
    rate = failures / calls
    return rate if rate > 0.1 else None


RULES: tuple[Rule, ...] = (
    Rule(
        name="CallersAreBeingCutOff",
        severity=CRITICAL,
        why=(
            "A session ceiling ended a call mid-conversation. Either one ran "
            "away or the ceiling is set wrong; both need a person."
        ),
        check=_cut_off_callers,
        expression="increase(voice_calls_stopped_by_budget_total[15m]) > 0",
        sustained="0m",
    ),
    Rule(
        name="CallsAreFailing",
        severity=CRITICAL,
        why=(
            "More than one call in twenty ended in an error. This is the same "
            "figure docs/ROLLBACK.md rolls back on."
        ),
        check=_failure_rate,
        expression=(
            "increase(voice_calls_failed_total[15m]) "
            "/ clamp_min(increase(voice_calls_total[15m]), 1) > 0.05"
        ),
    ),
    Rule(
        name="TheAgentIsSlow",
        severity=WARNING,
        why=(
            "p95 first audio over 2 s, the Phase 1 target. A caller hears this "
            "as a line that has gone dead."
        ),
        check=_p95_first_audio,
        expression=(
            "histogram_quantile(0.95, "
            "sum(rate(voice_first_audio_ms_bucket[15m])) by (le)) > 2000"
        ),
        sustained="10m",
    ),
    Rule(
        name="ACallIsRunningAway",
        severity=CRITICAL,
        why=(
            "p95 cost at or over the Rs 8 session ceiling. Deliberately not "
            "the Rs 2.50 target: every call is over that today, and an alert "
            "that fires every time is one people turn off."
        ),
        check=_runaway_cost,
        expression=(
            "histogram_quantile(0.95, "
            "sum(rate(voice_rupees_per_minute_bucket[15m])) by (le)) >= 8"
        ),
    ),
    Rule(
        name="UpstreamIsStruggling",
        severity=WARNING,
        why=(
            "More than one tool call in ten failed. The agent then says it "
            "could not check, which is honest and does not sell anything."
        ),
        check=_upstream,
        expression=(
            "increase(tool_failures_total[15m]) "
            "/ clamp_min(increase(tool_calls_total[15m]), 1) > 0.1"
        ),
    ),
)


def evaluate(metrics: Metrics | None = None) -> list[tuple[Rule, float]]:
    """Which rules are breaching right now, in this process."""
    source = metrics or METRICS
    firing = []
    for rule in RULES:
        value = rule.check(source)
        if value is not None:
            firing.append((rule, value))
    return firing


def announce(metrics: Metrics | None = None) -> None:
    """Write one ALERT line per breach, for a log-based alert to match.

    One line, one rule, the value and the reason. A log search can turn
    this into a page today, which is the point: alerting should not have to
    wait for somebody to choose a monitoring product.
    """
    for rule, value in evaluate(metrics):
        log.error(
            "ALERT %s severity=%s value=%s - %s",
            rule.name,
            rule.severity,
            round(value, 3),
            rule.why,
        )


def rules_yaml() -> str:
    """The same thresholds, as Prometheus alerting rules.

    Generated rather than written, so the file that fires cannot drift from
    the definitions above it. `make alerts` writes it; a test fails if the
    committed copy is stale.
    """
    lines = [
        "# Generated by `make alerts` from src/observability/alerts.py.",
        "# Do not edit: change the rule there and regenerate, so the",
        "# threshold that fires is the one that was reviewed.",
        "groups:",
        "  - name: poorvika-voice",
        "    rules:",
    ]
    for rule in RULES:
        lines += [
            f"      - alert: {rule.name}",
            f"        expr: {rule.expression}",
            f"        for: {rule.sustained}",
            "        labels:",
            f"          severity: {rule.severity}",
            "        annotations:",
            f"          summary: {rule.why}",
            "          runbook: docs/ROLLBACK.md",
        ]
    return "\n".join(lines) + "\n"
