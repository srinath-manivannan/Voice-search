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
    # Chat deliberately runs on a cheaper text model. See CHAT-PLAN.md.
    gemini_text_model: str

    # Phase 0 measured both of these. They are the two cheapest levers we have
    # and they belong in config, not hard-coded, so a change is reviewable.
    reply_word_cap: int
    chat_word_cap: int
    thinking_budget: int

    capture_sample_rate: int
    playback_sample_rate: int

    # Assumptions, not measurements. Named so a reviewer can see what the cost
    # report rests on.
    usd_inr: float
    gst_rate: float

    # Ceilings that end a session. A Google Cloud budget only emails; these
    # actually stop it.
    max_session_turns: int
    max_session_minutes: float
    max_session_inr: float

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
        playback_sample_rate=int(os.environ.get("PLAYBACK_SAMPLE_RATE", "24000")),
        usd_inr=float(os.environ.get("USD_INR", "89.0")),
        max_session_turns=int(os.environ.get("MAX_SESSION_TURNS", "40")),
        max_session_minutes=float(os.environ.get("MAX_SESSION_MINUTES", "10")),
        max_session_inr=float(os.environ.get("MAX_SESSION_INR", "20")),
        gst_rate=float(os.environ.get("GST_RATE", "0.18")),
        log_level=os.environ.get("LOG_LEVEL", defaults.LOG_LEVEL),
        env=env,
    )
