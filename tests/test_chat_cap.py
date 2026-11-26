"""The Chat tab stops starting paid answers at its cap (owner, 2026-11-24)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from src.config import load
from src.web.services import chat_relay


class Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class Usage:
    def __init__(self, usd: float) -> None:
        self.total_usd = usd


class Session:
    def __init__(self, usd: float) -> None:
        self.usage = Usage(usd)
        self.asked = 0

    async def send(self, text: str):
        self.asked += 1
        yield "an answer"

    @property
    def result(self):
        class Result:
            turns: list = []

        return Result()


def relay(monkeypatch, spent_usd: float, cap: float):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    settings = replace(load("development"), chat_session_cap_inr=cap)
    made = chat_relay.ChatRelay.__new__(chat_relay.ChatRelay)
    made._socket = Socket()
    made._settings = settings
    made._session = Session(spent_usd)
    return made


def test_past_the_cap_no_model_is_asked_and_the_way_to_guided_is_said(monkeypatch):
    capped = relay(monkeypatch, spent_usd=0.02, cap=1.00)  # about ₹2.10 spent

    asyncio.run(capped._answer("one more question"))

    assert capped._session.asked == 0
    texts = [m.get("text") for m in capped._socket.sent if m["type"] == "delta"]
    assert texts == [chat_relay.CAPPED_REPLY]
    assert capped._socket.sent[-1]["capped"] is True


def test_under_the_cap_the_model_answers(monkeypatch):
    fresh = relay(monkeypatch, spent_usd=0.0, cap=1.00)

    asyncio.run(fresh._answer("hello"))

    assert fresh._session.asked == 1
