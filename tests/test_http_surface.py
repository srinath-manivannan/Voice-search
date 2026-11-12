"""The HTTP surface itself: health, headers, versioning, rate limits.

None of this is about what the agent says. It is about what an operator and a
browser can rely on from the process - and most of it exists because the
surface had none of it, which is a thing you only notice from an incident.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.web.app import create_app
from src.web.middleware import REQUEST_ID_HEADER, SECURITY_HEADERS
from src.web.ratelimit import RateLimiter


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return load("development")


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as made:
        yield made


# --- health -----------------------------------------------------------------


def test_liveness_answers_without_touching_anything_downstream(client):
    """A load balancer polls this every few seconds.

    If it called Gemini it would both cost money and take the container out
    for somebody else's outage.
    """
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_names_the_capability_that_is_missing(client):
    """ "Not ready" is not an answer anyone can act on.

    "The catalogue token did not arrive" is. The check reports which
    capability is absent and never the value of anything.
    """
    body = client.get("/readyz").json()
    assert set(body["checks"]) == {"model_key", "catalogue", "estate"}
    assert body["ready"] is all(body["checks"].values())
    assert "test-key-not-real" not in str(body)


# --- what every response carries --------------------------------------------


def test_every_response_carries_a_request_id(client):
    """One call makes dozens of log lines across several modules.

    Without a handle tying them together, "which call was that?" is answered
    by guessing at timestamps.
    """
    response = client.get("/healthz")
    assert response.headers[REQUEST_ID_HEADER]


def test_a_request_id_we_were_given_is_the_one_we_report(client):
    """So a trace id from a gateway survives into our logs rather than
    being replaced by one nobody upstream has ever seen."""
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: "abc123"})
    assert response.headers[REQUEST_ID_HEADER] == "abc123"


@pytest.mark.parametrize("header", sorted(SECURITY_HEADERS))
def test_security_headers_are_present_on_every_response(client, header):
    assert client.get("/healthz").headers.get(header) == SECURITY_HEADERS[header]


def test_the_page_is_never_cached(client):
    """It is edited while it runs; a cached app.js reads as "my change did
    nothing", which costs more time than the caching ever saves."""
    assert "no-store" in client.get("/").headers["Cache-Control"]


# --- versioning -------------------------------------------------------------


def test_the_config_endpoint_is_versioned(client):
    """A breaking change to this shape must be able to ship beside the old
    one rather than instead of it."""
    assert client.get("/api/v1/config").status_code == 200
    assert client.get("/api/config").status_code == 404


# --- rate limiting ----------------------------------------------------------


def test_a_client_may_not_open_unlimited_calls():
    """This is a cost control before it is an abuse control.

    A session holds a socket open for minutes and spends money the whole
    time, so a page reconnecting in a loop is a bill, not just load.
    """
    limiter = RateLimiter(max_per_minute=3)
    assert [limiter.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]


def test_the_window_slides_rather_than_locking_a_client_out(client):
    limiter = RateLimiter(max_per_minute=2, window_seconds=60)
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("a", now=1.0)
    assert not limiter.allow("a", now=2.0)
    # A minute later the earliest start has aged out.
    assert limiter.allow("a", now=61.5)


def test_one_client_over_the_limit_does_not_block_another():
    limiter = RateLimiter(max_per_minute=1)
    assert limiter.allow("noisy")
    assert not limiter.allow("noisy")
    assert limiter.allow("quiet")


def test_a_limit_of_zero_means_no_limit_rather_than_no_service():
    """Misreading this is how a deployment with the setting unset answers
    nothing at all."""
    limiter = RateLimiter(max_per_minute=0)
    assert all(limiter.allow("anyone") for _ in range(50))
