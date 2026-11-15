"""The reply cap, and the three places it is set.

`.env.example` said 100 while `devconfig` and `prodconfig` said 15. An env
var wins over a shipped default, so anyone who copied the example ran at
nearly seven times the modelled reply length - and every cost figure in this
project described a configuration nobody was running.

Nothing caught it because each file was internally consistent and no test
ever compared them. This one does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.config import devconfig, prodconfig

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

CAPS = ("REPLY_WORD_CAP", "CHAT_WORD_CAP")


def from_example(name: str) -> int:
    """The value a developer gets by copying .env.example, uncommented."""
    for line in EXAMPLE.read_text().splitlines():
        if match := re.match(rf"^{name}=(\d+)\s*$", line.strip()):
            return int(match.group(1))
    raise AssertionError(f"{name} is not set in .env.example")


@pytest.mark.parametrize("name", CAPS)
def test_the_example_matches_the_shipped_default(name: str):
    """A copied example must not quietly change what the agent costs."""
    assert from_example(name) == getattr(prodconfig, name), (
        f"{name} differs between .env.example and prodconfig.py - an env var "
        "wins, so the shipped figure would describe a config nobody runs"
    )


@pytest.mark.parametrize("name", CAPS)
def test_both_environments_agree(name: str):
    """Development and production may differ in hosts, never in cost shape.

    A reply length that changes between them makes every measurement taken
    in development untrue of production, which is where the figures are
    quoted from.
    """
    assert getattr(devconfig, name) == getattr(prodconfig, name)


def test_the_voice_cap_fits_the_reply_the_persona_asks_for():
    """Two products, a reason each and one question is about 18 words.

    Measured 2026-11-15: at 15 the model wrote 5.1-second replies, at 25 it
    is modelled at 8.5 - the cap is a target in practice, not a ceiling, and
    audio out is the one line nothing discounts. 25 costs Rs 0.79 a minute
    more than 15; this pins the choice rather than the cost.
    """
    assert prodconfig.REPLY_WORD_CAP >= 18


def test_the_cap_reaches_the_instruction():
    from src.agent.persona import system_instruction

    assert f"under {prodconfig.REPLY_WORD_CAP} words" in system_instruction(
        "voice", prodconfig.REPLY_WORD_CAP
    )
