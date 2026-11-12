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


# --- one error shape --------------------------------------------------------


def test_a_missing_page_answers_in_the_same_shape_as_the_sockets(client):
    """The page had two failure shapes to parse for one service.

    Sockets said {"type": "error", "message": ...}; HTTP said
    {"detail": ...}, and nobody had written the second one down.
    """
    body = client.get("/no-such-thing").json()
    assert body["type"] == "error"
    assert isinstance(body["message"], str) and body["message"]
    assert body["request_id"]


def test_an_error_carries_the_request_id_that_is_in_the_log(client):
    """So the sentence on screen and the stack trace can be joined up by
    somebody who has only one of them."""
    response = client.get("/no-such-thing", headers={REQUEST_ID_HEADER: "trace-me"})
    assert response.json()["request_id"] == "trace-me"
    assert response.headers[REQUEST_ID_HEADER] == "trace-me"


def test_the_status_code_is_still_http_s_own(client):
    """Rule 25 asks for consistent status codes, not invented ones. What a
    404 means is HTTP's business."""
    assert client.get("/no-such-thing").status_code == 404
    assert client.post("/healthz").status_code == 405


def test_an_unexpected_failure_says_one_safe_sentence_and_logs_the_rest(config):
    """An exception's text is written for whoever wrote the code, and
    regularly carries a URL, a payload or a token. It does not go on screen.
    """
    from fastapi import FastAPI

    from src.web.errors import UNEXPECTED

    app: FastAPI = create_app(config)

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("token=sk-do-not-print-this and a stack full of paths")

    with TestClient(app, raise_server_exceptions=False) as made:
        response = made.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "type": "error",
        "message": UNEXPECTED,
        "request_id": body["request_id"],
    }
    assert "sk-do-not-print-this" not in response.text


# --- knowing what is running ------------------------------------------------


def test_the_health_endpoint_says_which_release_it_is(client):
    """The first question of every rollback is what is actually running.

    Answering it from a deployment log is how a rollback gets done twice: a
    log says what was asked for, this says what answered.
    """
    body = client.get("/healthz").json()
    assert "release" in body
    assert body["release"] == "unknown"  # not built as an image


def test_the_release_comes_from_the_environment_the_image_sets(monkeypatch):
    """Baked in at build time by deploy.sh, CI and `make image` alike, so a
    tag that says one thing while the process says another is caught at
    build rather than during an incident."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_RELEASE", "4f9c2a1b")
    from src.config import load

    with TestClient(create_app(load("development"))) as made:
        assert made.get("/healthz").json()["release"] == "4f9c2a1b"


def test_the_release_is_a_build_id_and_not_the_whole_stack(client):
    """A short sha of a private repository is not a secret. The model, the
    vendor and the word caps still are, and /healthz must not drift into
    publishing them the way /api/v1/config was never allowed to."""
    serialised = str(client.get("/healthz").json()).lower()
    for leak in ("gemini", "model", "api_key", "flash", "token"):
        assert leak not in serialised
