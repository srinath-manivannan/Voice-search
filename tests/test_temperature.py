"""Which temperature each half of a turn runs at, and why it is not a saving.

Step four of docs/CASCADE.md was written as "thinking off, temperature
routed, Rs 0.32 a minute". Half of that was already true and the other half
costs nothing either way:

  - Flash-Lite does not think. Measured 2026-11-15 over a two-turn
    conversation with tools, `thoughtsTokenCount` came back 0, and the field
    that would turn thinking off is rejected outright with a 400. So the
    Rs 0.32 the Live path spends on thinking is never incurred here - it was
    already inside the cascade's figure and claiming it again would have
    described the same rupee twice.
  - Temperature does not change what anything costs. The same tokens are
    generated and billed at the same rate at 0.0 as at 1.0.

What is left is a hallucination control, which is what it was always worth.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from src.chat.gemini_text import GeminiTextModel
from src.chat.session import QUOTING_TEMPERATURE, READING_TEMPERATURE, ChatSession
from src.voice.cascade import MOUTH_TEMPERATURE


class FakeResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class RecordingClient:
    """Answers with a scripted reply per call and keeps every request body."""

    def __init__(self, scripts: list[list[str]]) -> None:
        self._scripts = scripts
        self.sent: list[dict[str, Any]] = []

    @asynccontextmanager
    async def stream(self, method, url, *, params=None, json=None, headers=None):
        self.sent.append(json)
        script = self._scripts.pop(0) if self._scripts else []
        yield FakeResponse(script)


def sse(*chunks: dict[str, Any]) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks]


SAYS = sse({"candidates": [{"content": {"parts": [{"text": "sari"}]}}]})
CALLS_A_TOOL = sse(
    {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "search_products",
                                "args": {"category": "phone", "max_price": 20000},
                            }
                        }
                    ]
                }
            }
        ]
    }
)


class FakeTools:
    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"total_matching": 1, "products": [{"ref": "1", "name": "Poco M8"}]}


def model_with(client: RecordingClient) -> GeminiTextModel:
    return GeminiTextModel(
        api_key="k",
        model="flash-lite",
        system_instruction="be brief",
        streamer_factory=lambda: client,
        tools={"functionDeclarations": []},
    )


def temperatures(client: RecordingClient) -> list[float]:
    return [body["generationConfig"]["temperature"] for body in client.sent]


class TestTheRouting:
    async def test_a_turn_with_no_tool_call_runs_at_reading_temperature(self):
        client = RecordingClient([SAYS])
        session = ChatSession(model_with(client))

        async for _ in session.send("vanakkam"):
            pass

        assert temperatures(client) == [READING_TEMPERATURE]

    async def test_the_answer_after_a_tool_runs_at_quoting_temperature(self):
        """The round is the router, and asking it costs nothing.

        Round zero is the model reading a caller whose Tanglish arrives
        misspelt and half-finished. Every round after it has the tool's
        result in the window, so every number it could need is already in
        front of it.
        """
        client = RecordingClient([CALLS_A_TOOL, SAYS])
        session = ChatSession(model_with(client), tools=FakeTools())

        async for _ in session.send("20000 kulla phone"):
            pass

        assert temperatures(client) == [READING_TEMPERATURE, QUOTING_TEMPERATURE]

    async def test_quoting_leaves_nothing_to_invent(self):
        assert QUOTING_TEMPERATURE == 0.0
        assert 0.0 < READING_TEMPERATURE < 0.5


class TestWhatIsNotSent:
    async def test_no_thinking_config_reaches_flash_lite(self):
        """It is rejected outright with a 400 INVALID_ARGUMENT.

        The voice path sets thinkingBudget 0 because the Live model thinks by
        default and bills for it. Copying that across cost an hour once, and
        this is the guard so it costs nobody a second one.
        """
        client = RecordingClient([SAYS])
        session = ChatSession(model_with(client))

        async for _ in session.send("vanakkam"):
            pass

        assert "thinkingConfig" not in client.sent[0]["generationConfig"]
        assert "thinkingConfig" not in json.dumps(client.sent[0])


class TestTheMouth:
    def test_a_loudspeaker_has_nothing_to_be_creative_about(self):
        """It is handed a finished reply; variation can only be a wrong price."""
        assert MOUTH_TEMPERATURE == 0.0

    async def test_the_chat_tab_keeps_its_own_default(self):
        """Routing belongs to the voice brain. Chat passes no temperature and
        gets the model's own, which is what it had before any of this."""
        client = RecordingClient([SAYS])
        model = GeminiTextModel(
            api_key="k",
            model="flash-lite",
            system_instruction="be brief",
            streamer_factory=lambda: client,
        )

        async for _ in model.stream([{"role": "user", "parts": [{"text": "hi"}]}]):
            pass

        assert temperatures(client) == [0.7]
