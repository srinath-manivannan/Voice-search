"""Chat adapter and session.

The key-handling test here is not ceremony. The first version put the key in
the query string, a 400 came back, httpx put the whole URL in its exception,
and the secret was written to the terminal in full. That is the failure this
asserts against.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from src.agent.persona import CHAT_WORD_CAP, VOICE_WORD_CAP, system_instruction
from src.chat.gemini_text import GeminiTextModel, UpstreamError
from src.chat.session import ChatSession


class FakeResponse:
    def __init__(self, lines: list[str], status: int = 200, body: bytes = b"") -> None:
        self._lines = lines
        self.status_code = status
        self._body = body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    @asynccontextmanager
    async def stream(self, method, url, *, params=None, json=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
            }
        )
        yield self._response


def sse(*chunks: dict) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks]


REPLY = sse(
    {"candidates": [{"content": {"parts": [{"text": "Vanakkam! "}]}}]},
    {
        "candidates": [{"content": {"parts": [{"text": "Check panni sollren."}]}}],
        "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 9},
    },
)


def model_with(response: FakeResponse) -> tuple[GeminiTextModel, FakeClient]:
    client = FakeClient(response)
    return (
        GeminiTextModel(
            "secret-key", "gemini-3.5-flash-lite", "be helpful", lambda: client
        ),
        client,
    )


async def test_the_key_goes_in_a_header_and_never_into_the_url():
    model, client = model_with(FakeResponse(REPLY))
    async for _ in model.stream([{"role": "user", "parts": [{"text": "hi"}]}]):
        pass

    call = client.calls[0]
    assert call["headers"]["x-goog-api-key"] == "secret-key"
    # A URL reaches exception text, access logs and proxy logs. A header does not.
    assert "secret-key" not in call["url"]
    assert "secret-key" not in json.dumps(call["params"])


async def test_thinking_config_is_not_sent_because_flash_lite_rejects_it():
    """Copying the voice path's thinkingBudget here produced a 400."""
    model, client = model_with(FakeResponse(REPLY))
    async for _ in model.stream([{"role": "user", "parts": [{"text": "hi"}]}]):
        pass
    assert "thinkingConfig" not in json.dumps(client.calls[0]["json"])


async def test_an_upstream_error_reports_the_body_and_not_the_url():
    model, _ = model_with(
        FakeResponse([], status=400, body=b'{"error":"bad argument"}')
    )
    with pytest.raises(UpstreamError) as caught:
        async for _ in model.stream([{"role": "user", "parts": [{"text": "hi"}]}]):
            pass

    assert "bad argument" in str(caught.value)
    assert "secret-key" not in str(caught.value)


async def test_text_streams_in_pieces_and_the_turn_carries_usage():
    model, _ = model_with(FakeResponse(REPLY))
    deltas, turn = [], None
    async for delta, streamed in model.stream(
        [{"role": "user", "parts": [{"text": "hi"}]}]
    ):
        deltas.append(delta)
        turn = streamed

    # A trailing empty delta is deliberate: the turn is always handed back
    # once at the end, because a reply that is only a functionCall produces no
    # text and would otherwise never reach the caller at all.
    assert [d for d in deltas if d] == ["Vanakkam! ", "Check panni sollren."]
    assert deltas[-1] == ""
    assert turn is not None and turn.text == "Vanakkam! Check panni sollren."
    assert turn.usage["promptTokenCount"] == 120


async def test_session_keeps_history_and_prices_the_turn():
    model, _ = model_with(FakeResponse(REPLY))
    session = ChatSession(model)
    async for _ in session.send("budget phone venum"):
        pass

    assert [m["role"] for m in session.history] == ["user", "model"]
    assert session.result.turns[0].response_tokens == 9
    # Priced at Flash-Lite rates, not the Live model's.
    assert session.usage.output_usd == 9 * 2.50 / 1e6


async def test_history_is_trimmed_in_whole_exchanges():
    """Cutting mid-pair leaves a reply whose question is gone."""
    model, _ = model_with(FakeResponse(REPLY))
    session = ChatSession(model, max_history_messages=4)
    for _ in range(5):
        async for _ in session.send("again"):
            pass

    assert len(session.history) <= 6
    # Never starts on a model turn - that reads as the assistant volunteering.
    assert session.history[0]["role"] == "user"


def test_both_surfaces_share_one_persona_differing_only_in_delivery():
    voice = system_instruction("voice", VOICE_WORD_CAP)
    chat = system_instruction("chat", CHAT_WORD_CAP)

    for text in (voice, chat):
        assert "Never invent a price" in text
        # Tamil script by default, Tanglish when they write in Latin letters,
        # English never - Poorvika's customers are Tamil speakers.
        assert "NEVER REPLY IN ENGLISH" in text
        assert "Tanglish" in text
        # The greeting must survive the word cap, or it is spoken half-finished.
        assert "does NOT apply" in text
    assert "speaking out loud" in voice
    assert "chat window" in chat
    # Both caps are 100 at the operator's instruction. Measurement says the
    # model treats the cap as a ceiling rather than a target - 23 words a reply
    # at 100, against 15.3 at a cap of 15 - so raising it buys finished answers
    # rather than longer ones, and costs nothing measurable.
    assert CHAT_WORD_CAP == VOICE_WORD_CAP == 100


async def test_a_reply_that_is_only_a_tool_call_still_reaches_the_caller():
    """It produced nothing at all before.

    Gemini answers a product question with a functionCall part and no text.
    The stream yielded only on text, so the loop never ran, the caller never
    saw the turn, and the agent appeared to say nothing.
    """
    call_only = sse(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "search_products",
                                    "args": {"max_price": 30000},
                                },
                                "thoughtSignature": "sig-abc",
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }
    )
    model, _ = model_with(FakeResponse(call_only))
    turn = None
    async for _delta, streamed in model.stream(
        [{"role": "user", "parts": [{"text": "hi"}]}]
    ):
        turn = streamed

    assert turn is not None
    assert turn.function_call["args"]["max_price"] == 30000
    # The whole part, signature included - Gemini rejects the follow-up with a
    # 400 if the thoughtSignature is not echoed back.
    assert turn.function_call_part["thoughtSignature"] == "sig-abc"
