"""The cost payload the screen draws, assembled once.

It was built twice - once per turn and once at the end of the call - and the
two copies had already started to differ, which is how a summary quietly
stops matching the live figure it is supposed to total. One function now, and
the two callers differ only in which minute count they hand it.

What goes on the wire and why:

- every charge line, in rupees a connected minute, GST included
- the TARGET, which the screen colours against
- the CEILING, which ends the session, and is far above the target on purpose
- the voice floor: what the two audio streams alone cost, counted once each.
  Printed beside the total so it is visible how much of a call is physics and
  how much is re-processing, because only the second kind can be engineered
  away.
"""

from __future__ import annotations

from typing import Any

from src.config import Config
from src.voice.usage import SessionUsage


def report(usage: SessionUsage, settings: Config, minutes: float) -> dict[str, Any]:
    """Every charge line plus the two numbers it is read against."""
    payload = usage.breakdown(
        settings.usd_inr,
        settings.gst_rate,
        minutes,
        include_grok=settings.grok_memory_enabled,
    ).as_dict()
    payload["target_inr_per_min"] = settings.target_inr_per_minute
    payload["ceiling_inr_per_min"] = settings.max_inr_per_minute
    payload["voice_floor_inr_per_min"] = round(
        usage.voice_inr_per_minute(settings.usd_inr, settings.gst_rate, minutes), 3
    )
    return payload


def spent_inr(usage: SessionUsage, settings: Config) -> float:
    """Everything this session has cost so far, GST included."""
    return usage.total_usd * settings.usd_inr * (1 + settings.gst_rate)
