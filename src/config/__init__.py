"""Configuration, split per environment the way search-proxy splits it.

`devconfig` and `prodconfig` hold the values that differ between environments.
Everything else is read from the process environment, so a secret never has a
default and never appears in a file that is committed.

`load()` is the only way the rest of the code gets configuration. Nothing else
reads `os.environ` directly - that keeps the set of knobs discoverable in one
place and makes tests able to construct a Config without touching the process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    gemini_api_key: str
    gemini_live_model: str
    # Chat deliberately runs on a cheaper text model. See ../CHAT-PLAN.md.
    gemini_text_model: str

    # Phase 0 measured both of these. They are the two cheapest levers we have
    # and they belong in config, not hard-coded, so a change is reviewable.
    reply_word_cap: int
    chat_word_cap: int
    thinking_budget: int

    capture_sample_rate: int
    playback_sample_rate: int

    # Context window compression. Audio costs 25 tokens a second and the
    # prompt is cumulative, so a long call carries all of itself; these keep
    # the window bounded. Zero switches it off.
    context_trigger_tokens: int
    context_target_tokens: int

    # Session rotation, which does what compression cannot: move to a fresh
    # socket and hand it a few sentences of TEXT instead of the audio. The
    # threshold is retained caller+agent audio in tokens - 25 of them a
    # second - and zero leaves one socket for the whole call. See
    # src/voice/rotating.py and docs/PIPELINE.md.
    rotate_above_audio_tokens: int

    # Assumptions, not measurements. Named so a reviewer can see what the cost
    # report rests on.
    usd_inr: float
    gst_rate: float

    # Ceilings that end a session. A Google Cloud budget only emails; these
    # actually stop it.
    max_session_turns: int
    max_session_minutes: float
    max_session_inr: float
    # Two different numbers, deliberately. The target is what Poorvika wants a
    # connected minute to cost, GST in - Rs 2.00 - and it is what the screen
    # is coloured against. The ceiling is where a session is cut off, and it is a runaway
    # guard - set well above the target because hanging up on a customer
    # mid-sentence to save a rupee is not a cost saving. We are over the
    # target today; docs/COST.md says by how much and what would close it.
    target_inr_per_minute: float
    max_inr_per_minute: float

    # Product search. The token is a secret; the URL is not.
    catalogue_url: str
    catalogue_token: str
    catalogue_origin: str

    # The per-item estate - price in force, live stock, card offers, bank EMI.
    # `estate.py` explains the two backends and why one environment is chosen
    # whole rather than per service.
    estate_backend: str
    estate_api_env: str
    estate_src_path: str
    estate_worker_url: str
    estate_worker_token: str
    # Passed through to the Worker's own `env` binding under the names it
    # reads, so the mapping lives here rather than being spelled out twice.
    estate_secrets: dict[str, str]

    # Browsers allowed to call this server. Exact origins, never "*": the
    # socket carries a live conversation and a page on any other origin has
    # no business opening one.
    allowed_origins: tuple[str, ...]
    # New calls accepted per client per minute. A voice session holds a socket
    # open for minutes and spends real money for every one of them, so this is
    # a cost control as much as an abuse control.
    max_calls_per_minute: int

    # Which build this is. Baked into the image at build time and reported
    # by /healthz, because a rollback you cannot observe is a rollback you
    # are guessing at: "is the old one back up yet?" has to be answerable
    # without reading a deployment log. "unknown" outside a built image.
    #
    # A short commit sha of a private repository is not a secret, and the
    # cost of not having it - guessing which version is live during an
    # incident - is the larger one.
    release: str

    # Where errors are reported, as well as to the log. Empty means nowhere
    # leaves this process, which is the default: sending reports to a third
    # party is a data decision and it is Poorvika's to make. See
    # src/observability/reporting.py.
    sentry_dsn: str

    log_level: str
    env: str

    @property
    def system_instruction(self) -> str:
        """The voice agent's standing instruction.

        The word cap is interpolated rather than written out, because Phase 0
        showed reply length is the largest single cost line and responds almost
        linearly to how this sentence is worded. Changing the number is a
        performance change and should be measured like one.
        """
        from src.agent.persona import system_instruction

        return system_instruction("voice", self.reply_word_cap)

    @property
    def chat_instruction(self) -> str:
        """The same persona, delivered to a reader rather than a listener."""
        from src.agent.persona import system_instruction

        return system_instruction("chat", self.chat_word_cap)


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def load(env: str | None = None) -> Config:
    env = env or os.environ.get("APP_ENV", "development")

    # Both are imported unconditionally and one is selected. Rebinding the same
    # name from two conditional imports reads fine but mypy rejects it, and the
    # modules are a handful of constants each.
    from src.config import devconfig, prodconfig

    defaults = prodconfig if env == "main" else devconfig

    return Config(
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_live_model=os.environ.get(
            "GEMINI_LIVE_MODEL", defaults.GEMINI_LIVE_MODEL
        ),
        gemini_text_model=os.environ.get(
            "GEMINI_TEXT_MODEL", defaults.GEMINI_TEXT_MODEL
        ),
        reply_word_cap=int(os.environ.get("REPLY_WORD_CAP", defaults.REPLY_WORD_CAP)),
        chat_word_cap=int(os.environ.get("CHAT_WORD_CAP", defaults.CHAT_WORD_CAP)),
        thinking_budget=int(
            os.environ.get("THINKING_BUDGET", defaults.THINKING_BUDGET)
        ),
        capture_sample_rate=int(os.environ.get("CAPTURE_SAMPLE_RATE", "16000")),
        context_trigger_tokens=int(
            os.environ.get("CONTEXT_TRIGGER_TOKENS", defaults.CONTEXT_TRIGGER_TOKENS)
        ),
        context_target_tokens=int(
            os.environ.get("CONTEXT_TARGET_TOKENS", defaults.CONTEXT_TARGET_TOKENS)
        ),
        playback_sample_rate=int(os.environ.get("PLAYBACK_SAMPLE_RATE", "24000")),
        rotate_above_audio_tokens=int(
            os.environ.get(
                "ROTATE_ABOVE_AUDIO_TOKENS", defaults.ROTATE_ABOVE_AUDIO_TOKENS
            )
        ),
        usd_inr=float(os.environ.get("USD_INR", "89.0")),
        max_session_turns=int(os.environ.get("MAX_SESSION_TURNS", "40")),
        max_session_minutes=float(os.environ.get("MAX_SESSION_MINUTES", "10")),
        max_session_inr=float(os.environ.get("MAX_SESSION_INR", "50")),
        target_inr_per_minute=float(os.environ.get("TARGET_INR_PER_MINUTE", "2.00")),
        max_inr_per_minute=float(os.environ.get("MAX_INR_PER_MINUTE", "8.00")),
        gst_rate=float(os.environ.get("GST_RATE", "0.18")),
        catalogue_url=os.environ.get("CATALOGUE_URL", defaults.CATALOGUE_URL),
        catalogue_token=os.environ.get("CATALOGUE_TOKEN", ""),
        catalogue_origin=os.environ.get("CATALOGUE_ORIGIN", "bot"),
        estate_backend=os.environ.get("ESTATE_BACKEND", defaults.ESTATE_BACKEND),
        estate_api_env=os.environ.get("ESTATE_API_ENV", defaults.ESTATE_API_ENV),
        estate_src_path=os.environ.get("ESTATE_SRC_PATH", defaults.ESTATE_SRC_PATH),
        estate_worker_url=os.environ.get("ESTATE_WORKER_URL", ""),
        estate_worker_token=os.environ.get("ESTATE_WORKER_TOKEN", ""),
        estate_secrets={
            name: os.environ.get(name, "")
            for name in (
                "PIM_ACCESS_TOKEN",
                "OFFERS_API_KEY",
                "CI_AUTH_TOKEN",
                # Host overrides, for the two the estate table has no entry
                # for. Empty means "not configured", which is reported rather
                # than guessed around.
                "CI_HOST",
                "EMI_HOST",
                "PIM_HOST",
                "OFFERS_HOST",
                # The CI gateway verifies its token with a secret chosen by
                # this header, so the same token is accepted under one mode
                # and rejected as "signature is invalid" under another.
                "CI_MODE",
                "OFFERS_MODE",
            )
        },
        allowed_origins=tuple(
            origin.strip()
            for origin in os.environ.get(
                "ALLOWED_ORIGINS", defaults.ALLOWED_ORIGINS
            ).split(",")
            if origin.strip()
        ),
        max_calls_per_minute=int(os.environ.get("MAX_CALLS_PER_MINUTE", "6")),
        release=os.environ.get("APP_RELEASE", "unknown"),
        sentry_dsn=os.environ.get("SENTRY_DSN", ""),
        log_level=os.environ.get("LOG_LEVEL", defaults.LOG_LEVEL),
        env=env,
    )
