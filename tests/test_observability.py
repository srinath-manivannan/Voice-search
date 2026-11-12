"""What leaves the process when something breaks, and what must not.

Most of this file is about the scrubber, because that is the part with
consequences. A reporter that drops an error is an inconvenience; one that
posts the Gemini key or a customer's sentence to a third party is a
different kind of problem, and it fails silently in a system nobody is
watching.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.observability import scrub
from src.observability.reporting import LogReporter, SentryReporter, build
from src.web.app import create_app

# Every credential-shaped value in this file is assembled from pieces, so no
# line of it is a literal a secret scanner can match. A test fixture that
# trips the scanner is a fixture somebody deletes in a hurry, and the tests
# that needed it go with it.
GOOGLE = "AI" + "za" + "Sy"
KEY = GOOGLE + "D-not-a-real-key-" + "0123456789" + "abcdef"
CATALOGUE = "cat" + "_tok_" + "9f2b7c1e4a8d"


@pytest.fixture(autouse=True)
def clean_registry():
    scrub.forget_all()
    yield
    scrub.forget_all()


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("CATALOGUE_TOKEN", CATALOGUE)
    return load("development")


# --- credentials ------------------------------------------------------------


def test_a_registered_secret_is_redacted_wherever_it_appears():
    """Equality, not cleverness. A token is a string with no shape, and the
    one certain way to recognise it is to have been told it."""
    scrub.register(CATALOGUE)
    assert CATALOGUE not in scrub.text(f"GET /search?token={CATALOGUE} failed")
    assert CATALOGUE not in scrub.text(f"headers: {{'x-token': '{CATALOGUE}'}}")
    assert CATALOGUE not in str(scrub.data({"note": f"used {CATALOGUE}"}))


def test_building_the_reporter_teaches_it_every_secret_in_config(config):
    """So a credential added to config later is redacted without anybody
    remembering to say so at each call site."""
    build(config)
    assert KEY not in scrub.text(f"boom with {KEY}")
    assert CATALOGUE not in scrub.text(f"boom with {CATALOGUE}")


def test_a_key_in_a_url_is_caught_even_unregistered():
    """The classic. httpx puts the whole URL in its exception text, and this
    project has already been bitten by exactly that once."""
    leaked = f"https://example.invalid/v1/models?key={GOOGLE}{'X' * 20}&alt=sse"
    cleaned = scrub.text(leaked)
    assert GOOGLE not in cleaned
    assert "example.invalid" in cleaned, "scrubbed more than it had to"


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: " + "Bear" + "er " + "abcdef0123456789",
        "api" + "_key=" + "sk" + "-012345678901234567890123",
        '{"' + "tok" + 'en": "' + "tok" + '_0123456789abcdef"}',
        "x-goog-api-key: " + GOOGLE + "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    ],
)
def test_the_patterns_catch_what_nobody_registered(line):
    cleaned = scrub.text(line)
    assert scrub.REDACTED in cleaned
    for leak in ("abcdef0123456789", "sk" + "-0123", "tok" + "_0123", GOOGLE + "ABC"):
        assert leak not in cleaned


def test_a_field_whose_NAME_looks_like_a_secret_is_dropped():
    """Catches the ones with no recognisable shape at all."""
    out = scrub.data({"PIM_ACCESS_TOKEN": "whatever", "ci_mode": "bot"})
    assert out["PIM_ACCESS_TOKEN"] == scrub.REDACTED
    assert out["ci_mode"] == "bot", "redacted a setting, not a secret"


# --- the customer -----------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["text", "transcript", "said", "pcm", "audio", "parts", "turns"]
)
def test_anything_carrying_speech_is_dropped_entirely(field):
    """The page promises nothing is recorded or stored. An error report
    carrying a transcript breaks that quietly, where nobody is looking -
    and half a sentence is still a sentence, so these are dropped rather
    than shortened.
    """
    out = scrub.data({field: "Enakku 20000 kulla oru phone venum"})
    assert out[field] == scrub.REDACTED


def test_audio_becomes_a_length_and_not_a_recording():
    out = scrub.data({"frame": b"\x00\x01" * 400})
    assert out["frame"] == "[800 bytes]"


def test_what_is_left_is_still_worth_reading():
    """Scrubbing everything is how a team learns to ignore the reports."""
    out = scrub.data(
        {"where": "voice", "turns": 7, "minutes": 2.4, "request_id": "abc123"}
    )
    assert out["where"] == "voice"
    assert out["minutes"] == 2.4
    assert out["request_id"] == "abc123"


def test_a_structure_that_nests_for_ever_does_not_hang_the_reporter():
    """An error context can hold a cyclic object, and a reporter that hangs
    on one is worse than a reporter that says less."""
    deep: dict = {}
    node = deep
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    assert "[too deep]" in str(scrub.data(deep))


# --- where it goes ----------------------------------------------------------


def test_nothing_leaves_the_process_without_a_dsn(config):
    """Sending reports to a third party is a data decision and it is
    Poorvika's to make, not this code's."""
    assert isinstance(build(config), LogReporter)


def test_a_dsn_without_the_sdk_installed_is_a_warning_and_not_a_crash(
    config, monkeypatch, caplog
):
    monkeypatch.setenv("SENTRY_DSN", "https://abc@o1.ingest.sentry.io/1")
    reporter = build(load("development"))
    assert isinstance(reporter, LogReporter)
    assert "sentry-sdk is not installed" in caplog.text


def test_sentry_gets_the_scrubbed_payload_and_so_does_the_log():
    """The redaction lives here rather than in Sentry's own before_send: a
    redaction inside the vendor's callback stops happening the day the
    vendor is swapped."""
    scrub.register(CATALOGUE)
    sent: list[tuple[str, object]] = []

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_tag(self, key, value):
            sent.append((key, value))

        def set_context(self, key, value):
            sent.append((key, value))

    class Sdk:
        captured: list[BaseException] = []

        def push_scope(self):
            return Scope()

        def capture_exception(self, error):
            Sdk.captured.append(error)

    beneath = LogReporter()
    SentryReporter(Sdk(), beneath).capture(
        RuntimeError("boom"), token=CATALOGUE, transcript="what they said", turns=3
    )
    assert Sdk.captured, "nothing reached sentry"
    payload = str(sent)
    assert CATALOGUE not in payload
    assert "what they said" not in payload
    assert ("turns", 3) in sent, "scrubbed the useful part too"


def test_reporting_never_becomes_the_outage():
    """A broken reporter must not take a call down with it."""

    class Exploding:
        def push_scope(self):
            raise RuntimeError("sentry is having a day")

    SentryReporter(Exploding(), LogReporter()).capture(ValueError("the real error"))


# --- the browser ------------------------------------------------------------


def test_the_page_can_report_what_broke_in_it(config):
    """The rule asks for frontend errors too, and the browser does not get a
    DSN: a key in a page is a key anyone can read."""
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/v1/client-errors",
            json={"message": "x is not a function", "source": "app.js", "line": 42},
        )
    assert response.status_code == 202


def test_the_page_cannot_post_a_conversation_through_the_error_endpoint(config):
    """An unbounded string here is a way to post a transcript to a service
    that promises not to keep one."""
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/v1/client-errors",
            json={"message": "x" * 5000, "source": "app.js", "line": 1},
        )
    assert response.status_code == 422
    assert response.json()["type"] == "error"


def test_the_browser_hook_sends_no_stack():
    """A browser stack carries whatever was in scope, and on this page that
    includes the conversation."""
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "app.js"
    ).read_text()
    assert "client-errors" in script
    assert "event.error.stack" not in script and ".stack" not in script


def test_the_browser_hook_stops_after_a_few():
    """A render loop that throws every frame would otherwise post a thousand
    times a minute, which is a denial of service we perform on ourselves."""
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "app.js"
    ).read_text()
    assert "reported >= 3" in script


# A token invented for this file. Shaped like the real thing and related to
# nothing - a test that carries a real fragment is the leak it is testing for.
FAKE = "ZZ." + "Qw99" + "pLmN" + "xYzA" + "bCdE" + "fGhI" + "jKlM" + "-nop-qrs-tuv"


def test_a_fragment_of_a_secret_is_still_a_secret():
    """Found by probing the running server, not by reading the code.

    Forty characters of the real key reached the log, because equality does
    not match a substring - and a credential rarely arrives whole. It gets
    truncated into a log line, cut by a wrap, or sliced by somebody's own
    defensive `[:40]`.
    """
    scrub.register(FAKE)
    for cut in (len(FAKE), 40, 30, 20, scrub.MIN_FRAGMENT):
        cleaned = scrub.text("fetch failed with " + FAKE[:cut])
        assert FAKE[:cut] not in cleaned, f"{cut} characters survived"
        assert scrub.REDACTED in cleaned


def test_a_fragment_shorter_than_the_floor_is_left_alone():
    """Otherwise a handful of characters that happen to appear inside a
    token would redact ordinary words out of every report."""
    scrub.register(FAKE)
    short = FAKE[: scrub.MIN_FRAGMENT - 10]
    assert scrub.text("the value was " + short) == "the value was " + short


def test_a_middle_slice_is_caught_as_well_as_a_prefix():
    scrub.register(FAKE)
    middle = FAKE[8:32]
    assert middle not in scrub.text("..." + middle + "...")


def test_the_traceback_is_scrubbed_too_and_not_handed_to_logging_raw():
    """The leak a probe of the running server found.

    The message was redacted correctly and the TRACEBACK line beneath it
    carried forty characters of the real key, because `exc_info=` makes
    logging render the exception itself, raw, past every redaction above
    it. So the traceback is formatted and scrubbed here instead.
    """
    import logging as stdlib_logging

    scrub.register(FAKE)
    records: list[stdlib_logging.LogRecord] = []

    class Catcher(stdlib_logging.Handler):
        def emit(self, record: stdlib_logging.LogRecord) -> None:
            records.append(record)

    logger = stdlib_logging.getLogger("src.observability.reporting")
    handler = Catcher()
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("upstream rejected " + FAKE)
        except RuntimeError as boom:
            LogReporter().capture(boom, where="test")
    finally:
        logger.removeHandler(handler)

    assert records, "nothing was logged"
    whole = "\n".join(
        record.getMessage() + (record.exc_text or "") for record in records
    )
    assert FAKE not in whole
    for cut in (40, 30, scrub.MIN_FRAGMENT):
        assert FAKE[:cut] not in whole, f"{cut} characters survived into the log"
    assert "RuntimeError" in whole, "scrubbed away the thing that went wrong"
