"""One real Grok summary. Excluded from the default run.

    XAI_API_KEY=... .venv/bin/python -m pytest -m live tests/test_grok_live.py

This spends a fraction of a cent. It is the only test that can catch a
renamed model or a usage block that moved. CI never runs it.
"""

from __future__ import annotations

import os
import time

import pytest
from dotenv import load_dotenv

from src.memory.grok import GrokClient
from src.memory.summariser import CallMemory

pytestmark = pytest.mark.live


async def test_grok_writes_a_handover_from_a_short_transcript():
    # Same loader the app uses in create_app. A key that lives only in .env
    # is not in the shell, and pytest does not export it on its own.
    load_dotenv()
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        pytest.skip("XAI_API_KEY not set")
    model = os.environ.get("GROK_MODEL", "grok-4.3")
    remembered = CallMemory(
        GrokClient(key, model, timeout_s=20),
        every=1,
        timeout_s=20,
    )
    remembered.add_caller("20,000 to 25,000 camera phone, Tanglish")
    remembered.add_agent(
        "Poco M8 5G, 19999 rupees. Realme P4, 20999. Edhu paakkalaam?",
        turn_id=1,
    )
    assert remembered.caller_reply_finished() is True
    await remembered.summarise()

    assert remembered.handover
    assert len(remembered.handover) <= 600
    lowered = remembered.handover.lower()
    assert "tanglish" in lowered or "tamil" in lowered
    # The system prompt is what asks for these lines. A model that ignored
    # it still has to have said something about the budget or the phone.
    assert "budget" in lowered or "poco" in lowered or "20" in remembered.handover


async def test_the_configured_model_answers_inside_the_configured_timeout():
    """The test that was missing, and what it would have caught.

    GROK_MODEL defaulted to grok-4.3, which is a REASONING model. On a
    four-line transcript it took 8,924 ms against a GROK_TIMEOUT_SECONDS of
    4.0, so every summary this service ever asked for timed out. The handover
    stayed empty, on_usage was never called, and the cost panel showed xAI at
    zero on every call - which read as "Grok is cheap" rather than "Grok has
    never run". The feature had not worked once.

    Nothing offline could see it. The timeout is a number in config, the
    latency is a property of a model on someone else's hardware, and only
    asking it compares the two.
    """
    load_dotenv()
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        pytest.skip("XAI_API_KEY not set")

    from src.config import load
    from src.memory.summariser import MAX_TOKENS, _messages

    settings = load()
    client = GrokClient(settings.xai_api_key, settings.grok_model, timeout_s=60)
    lines = [
        ("caller", "20-30k kulla oru phone venum"),
        ("agent", "Poco M8 5G, 19,999 rupees - 45MP camera."),
        ("caller", "stock irukka?"),
        ("agent", "Stock irukku, 24 branch-la."),
    ]

    started = time.perf_counter()
    completion = await client.chat(_messages(lines, ""), max_tokens=MAX_TOKENS)
    took = time.perf_counter() - started

    assert completion.text.strip(), "the model answered with nothing"
    assert took < settings.grok_timeout_seconds, (
        f"{settings.grok_model} took {took:.1f}s against a "
        f"{settings.grok_timeout_seconds}s timeout - every summary will time "
        f"out and the handover will stay empty"
    )
