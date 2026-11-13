"""One real Grok summary. Excluded from the default run.

    XAI_API_KEY=... .venv/bin/python -m pytest -m live tests/test_grok_live.py

This spends a fraction of a cent. It is the only test that can catch a
renamed model or a usage block that moved. CI never runs it.
"""

from __future__ import annotations

import os

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
