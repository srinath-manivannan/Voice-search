"""Where an error goes after it has been scrubbed.

Today it goes to the log and nowhere else, which is a gap rather than a
decision: a stack trace nobody reads at three in the morning is not
monitoring. This is the seam that closes it.

**Off unless a DSN is set.** Sending error reports to a third party is a
data-processing decision and it is Poorvika's to make, not this file's. With
no `SENTRY_DSN` nothing leaves the process and the behaviour is exactly what
it was. With one, the same scrubbed payload goes to Sentry as well as the
log - as well as, never instead of, because the log is the copy that stays
in the country the call happened in.

The SDK is imported lazily and its absence is not an error. The image can
ship without it, and a deployment that wants reporting installs it and sets
a DSN; neither decision reaches this code.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Protocol

from src.config import Config
from src.observability import scrub

log = logging.getLogger(__name__)


class Reporter(Protocol):
    """Somewhere an error can be sent."""

    def capture(self, error: BaseException, **context: Any) -> None: ...


class LogReporter:
    """The floor. Always on, even when something else is too.

    The log is the copy that stays on the machine the call happened on, and
    it is the one that still exists when the third party is unreachable or
    the account has lapsed.
    """

    def capture(self, error: BaseException, **context: Any) -> None:
        # The traceback is formatted and scrubbed HERE rather than handed to
        # logging as `exc_info`. Logging renders the exception itself, raw,
        # which walks straight past the redaction above it: a probe of the
        # running server found forty characters of the real key in the log,
        # on the traceback line, under a message that had been scrubbed
        # correctly.
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        log.error(
            "%s: %s | %s\n%s",
            type(error).__name__,
            scrub.text(str(error)),
            scrub.data(context),
            scrub.text(rendered),
        )


class SentryReporter:
    """Sentry, with the payload scrubbed before it is handed over.

    The scrubbing happens here rather than in Sentry's own `before_send`
    because a redaction that lives inside the vendor's callback is a
    redaction that stops happening the day the vendor is swapped.
    """

    def __init__(self, sdk: Any, beneath: Reporter) -> None:
        self._sdk = sdk
        self._beneath = beneath

    def capture(self, error: BaseException, **context: Any) -> None:
        self._beneath.capture(error, **context)
        try:
            with self._sdk.push_scope() as scope:
                for key, value in scrub.data(context).items():
                    scope.set_tag(key, value) if isinstance(
                        value, (str, int, float, bool)
                    ) else scope.set_context(key, value)
                self._sdk.capture_exception(error)
        except Exception:  # noqa: BLE001 - reporting must never be the outage
            log.debug("could not report to sentry", exc_info=True)


def build(settings: Config) -> Reporter:
    """The reporter this deployment should use.

    Also the one place that teaches the scrubber what the secrets are. Doing
    it here rather than at each call site means a credential added to config
    later is redacted without anybody remembering to say so.
    """
    scrub.register(
        settings.gemini_api_key,
        settings.catalogue_token,
        settings.estate_worker_token,
        *settings.estate_secrets.values(),
    )

    beneath = LogReporter()
    if not settings.sentry_dsn:
        return beneath

    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return beneath

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        release=settings.release,
        # Nothing about who is on the call. The page promises the audio is
        # not recorded, and an error report carrying an IP or a session is
        # the same promise broken somewhere nobody is looking.
        send_default_pii=False,
        # Tracing is a performance product and it samples request bodies.
        # Latency is already measured per turn, on screen, for free.
        traces_sample_rate=0.0,
        max_breadcrumbs=20,
    )
    log.info("errors will be reported to sentry as well as the log")
    return SentryReporter(sentry_sdk, beneath)
