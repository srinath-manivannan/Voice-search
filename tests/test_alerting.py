"""The numbers a monitor reads, and the thresholds it reads them against.

The thresholds are in code rather than in a dashboard so that the alert
which wakes somebody and the rule which tells them to roll back are the same
number. At three in the morning the argument should be about the call that
is failing, not about which of two figures is right. These tests hold the
two together.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.observability import alerts
from src.observability.metrics import LATENCY_BUCKETS_MS, RUPEE_BUCKETS, Metrics
from src.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return load("development")


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


# --- the exposition ---------------------------------------------------------


def test_a_counter_renders_as_prometheus_expects(metrics):
    metrics.count("voice_calls_total", "Voice calls accepted")
    metrics.count("voice_calls_total", "Voice calls accepted")
    text = metrics.render()
    assert "# TYPE voice_calls_total counter" in text
    assert "voice_calls_total 2" in text


def test_a_histogram_renders_cumulative_buckets(metrics):
    """Prometheus buckets are cumulative - each `le` counts everything at or
    under it. Rendering them as individual counts is silently wrong, and the
    quantile it computes is nonsense rather than an error."""
    for value in (100.0, 600.0, 3000.0):
        metrics.observe(
            "voice_first_audio_ms", "first audio", value, LATENCY_BUCKETS_MS
        )
    text = metrics.render()
    assert 'voice_first_audio_ms_bucket{le="250"} 1' in text
    assert 'voice_first_audio_ms_bucket{le="750"} 2' in text
    assert 'voice_first_audio_ms_bucket{le="+Inf"} 3' in text
    assert "voice_first_audio_ms_count 3" in text


def test_the_latency_buckets_bracket_the_targets_we_promise():
    """Phase 1 is held to p50 under 1 s and p95 under 2 s. A histogram whose
    edges do not bracket the number you promised cannot tell you whether you
    kept it."""
    assert 1000.0 in LATENCY_BUCKETS_MS
    assert 2000.0 in LATENCY_BUCKETS_MS


def test_the_rupee_buckets_bracket_the_target_and_the_ceiling():
    assert 2.0 in RUPEE_BUCKETS, "the Rs 2 target is not a bucket edge"
    assert 8.0 in RUPEE_BUCKETS, "the Rs 8 session ceiling is not a bucket edge"


def test_nothing_in_the_exposition_identifies_a_caller(metrics):
    """A label is a cardinality decision as well as a privacy one, and one
    per caller would be both."""
    metrics.count("voice_calls_total", "Voice calls accepted")
    metrics.observe("voice_rupees_per_minute", "cost", 3.0, RUPEE_BUCKETS)
    text = metrics.render()
    assert "{" not in text.replace('{le="', ""), "a label other than le appeared"


# --- when it fires ----------------------------------------------------------


def test_a_caller_cut_off_by_a_ceiling_wakes_somebody(metrics):
    """The rupee ceiling is a runaway guard set far above the target
    precisely so this does not happen."""
    assert not alerts.evaluate(metrics)
    metrics.count("voice_calls_stopped_by_budget_total", "stopped")
    firing = {rule.name for rule, _ in alerts.evaluate(metrics)}
    assert "CallersAreBeingCutOff" in firing


def test_being_over_the_rupee_TARGET_does_not_wake_anybody(metrics):
    """Every call is over Rs 2.00 today. An alert that fires every time is
    one people turn off, and the real ones go with it."""
    for _ in range(10):
        metrics.observe("voice_rupees_per_minute", "cost", 5.28, RUPEE_BUCKETS)
    assert not alerts.evaluate(metrics)


def test_being_over_the_rupee_CEILING_does(metrics):
    for _ in range(10):
        metrics.observe("voice_rupees_per_minute", "cost", 9.0, RUPEE_BUCKETS)
    firing = {rule.name for rule, _ in alerts.evaluate(metrics)}
    assert "ACallIsRunningAway" in firing


def test_slowness_is_measured_against_the_phase_1_target(metrics):
    for _ in range(10):
        metrics.observe("voice_first_audio_ms", "latency", 3000.0, LATENCY_BUCKETS_MS)
    firing = {rule.name for rule, _ in alerts.evaluate(metrics)}
    assert "TheAgentIsSlow" in firing


def test_a_fast_agent_is_quiet(metrics):
    for _ in range(50):
        metrics.observe("voice_first_audio_ms", "latency", 600.0, LATENCY_BUCKETS_MS)
    assert not alerts.evaluate(metrics)


def test_one_bad_call_out_of_three_is_not_yet_a_signal(metrics):
    """Alerting on a handful of calls is alerting on noise, and the first
    few calls after a deploy are exactly when noise is loudest."""
    for _ in range(3):
        metrics.count("voice_calls_total", "calls")
    metrics.count("voice_calls_failed_total", "failed")
    assert not alerts.evaluate(metrics)


def test_one_call_in_twenty_failing_is(metrics):
    """The same figure docs/ROLLBACK.md rolls back on."""
    for _ in range(40):
        metrics.count("voice_calls_total", "calls")
    for _ in range(3):
        metrics.count("voice_calls_failed_total", "failed")
    firing = {rule.name for rule, _ in alerts.evaluate(metrics)}
    assert "CallsAreFailing" in firing


def test_every_rule_can_say_why_it_exists():
    """An alert whose reason nobody can state is one that gets silenced the
    second time it fires."""
    for rule in alerts.RULES:
        assert len(rule.why) > 40, rule.name
        assert rule.severity in (alerts.CRITICAL, alerts.WARNING)


def test_breaches_are_announced_as_one_matchable_line(metrics, caplog):
    """Log-based alerting works today. A monitoring product has not been
    chosen, and waiting for one is how a service runs unwatched."""
    metrics.count("voice_calls_stopped_by_budget_total", "stopped")
    alerts.announce(metrics)
    assert "ALERT CallersAreBeingCutOff severity=critical" in caplog.text


# --- the generated rules ----------------------------------------------------


def test_the_committed_rules_file_matches_the_definitions():
    """Generated, not written, so the threshold that fires cannot drift from
    the one that was reviewed. If this fails, run `make alerts`."""
    committed = (ROOT / "deploy" / "alerts.yml").read_text()
    assert committed == alerts.rules_yaml(), "deploy/alerts.yml is stale"


def test_every_rule_reaches_the_generated_file():
    rendered = alerts.rules_yaml()
    for rule in alerts.RULES:
        assert f"alert: {rule.name}" in rendered
        assert rule.expression in rendered
    assert rendered.count("runbook: docs/ROLLBACK.md") == len(alerts.RULES)


def test_the_thresholds_agree_with_the_rollback_runbook():
    """The alert that wakes you and the rule that says roll back have to be
    the same number."""
    runbook = (ROOT / "docs" / "ROLLBACK.md").read_text()
    assert "1 in 20 calls" in runbook, "the failure rate moved in one place only"
    assert "₹8" in runbook, "the session ceiling moved in one place only"


# --- the endpoint -----------------------------------------------------------


def test_metrics_are_served_in_the_format_a_scrape_expects(config):
    with TestClient(create_app(config)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "version=0.0.4" in response.headers["content-type"]


def test_a_token_closes_the_endpoint_when_one_is_set(monkeypatch):
    """The numbers are not secrets but they are commercial. How many calls
    Poorvika takes is not for a public URL."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("METRICS_TOKEN", "a-long-enough-metrics-token")
    with TestClient(create_app(load("development"))) as client:
        assert client.get("/metrics").status_code == 404
        allowed = client.get(
            "/metrics", headers={"authorization": "Bearer a-long-enough-metrics-token"}
        )
    assert allowed.status_code == 200


def test_a_refused_scrape_looks_like_nothing_is_there(monkeypatch):
    """404 rather than 401: an endpoint that announces itself as protected
    is an endpoint somebody now knows to attack."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("METRICS_TOKEN", "a-long-enough-metrics-token")
    with TestClient(create_app(load("development"))) as client:
        refused = client.get("/metrics", headers={"authorization": "Bearer wrong"})
    assert refused.status_code == 404
    assert "metrics" not in refused.text.lower()


# --- resource use -----------------------------------------------------------


def test_the_process_reports_its_own_cpu_and_memory(metrics):
    """The rule asks for resource use watched, not only latency and errors.

    A profiler run on a laptop says what a call costs here, today. These
    say what it is costing in production, which is the version anybody is
    actually alerted on.
    """
    from src.observability.metrics import resources

    resources(metrics)
    text = metrics.render()
    assert "process_cpu_seconds_total" in text
    assert "process_resident_memory_bytes" in text


def test_resource_gauges_are_read_at_scrape_time_not_sampled(config):
    """A gauge only as fresh as a background task is one that lies after
    the task dies."""
    with TestClient(create_app(config)) as client:
        first = client.get("/metrics").text
        second = client.get("/metrics").text
    assert "process_cpu_seconds_total" in first
    assert "process_cpu_seconds_total" in second
