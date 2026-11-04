"""The adapter is tested against a scripted socket, so these cost nothing.

The server frames below are shaped like the ones the real API sent during the
Phase 0 measurement runs. A `live` test at the bottom exercises the real thing
and is excluded from the default run.
"""

from __future__ import annotations

import base64
import json
from collections import deque

import pytest

from src.config import load
from src.voice.events import AudioOut, Transcript, TurnComplete, TurnInterrupted
from src.voice.gemini_live import GeminiLiveSession, SetupFailed


class FakeSocket:
    """Records what the adapter sends; replays what we tell it the server said."""

    def __init__(self, inbound: list[dict]) -> None:
        self.sent: list[dict] = []
        self._inbound = deque(json.dumps(m) for m in inbound)
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if not self._inbound:
            raise StopAsyncIteration
        return self._inbound.popleft()

    async def close(self) -> None:
        self.closed = True


def connector(socket: FakeSocket):
    async def _connect(_url: str) -> FakeSocket:
        return socket

    return _connect


def audio_frame(pcm: bytes) -> dict:
    return {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "audio/pcm",
                            "data": base64.b64encode(pcm).decode(),
                        }
                    }
                ]
            }
        }
    }


USAGE = {
    "promptTokenCount": 645,
    "responseTokenCount": 103,
    "totalTokenCount": 748,
    "promptTokensDetails": [
        {"modality": "TEXT", "tokenCount": 335},
        {"modality": "AUDIO", "tokenCount": 283},
    ],
    "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 103}],
    "thoughtsTokenCount": 90,
}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return load("development")


async def test_setup_switches_thinking_off_explicitly(config):
    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)):
        pass
    generation = sock.sent[0]["setup"]["generationConfig"]
    # Explicit even when it matches the model default - leaving it out costs
    # money and latency, and the omission would be invisible in review.
    assert generation["thinkingConfig"]["thinkingBudget"] == 0


async def test_setup_carries_the_measured_word_cap(config):
    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)):
        pass
    instruction = sock.sent[0]["setup"]["systemInstruction"]["parts"][0]["text"]
    assert "under 15 words" in instruction
    assert "Never invent" in instruction


async def test_setup_rejection_raises_and_does_not_leak_the_key(config):
    sock = FakeSocket([{"error": {"code": 404, "message": "model not found"}}])
    with pytest.raises(SetupFailed) as caught:
        async with GeminiLiveSession(config, connect=connector(sock)):
            pass
    assert "test-key-not-real" not in str(caught.value)


async def test_audio_and_transcript_are_tagged_with_the_current_turn(config):
    sock = FakeSocket(
        [
            {"setupComplete": {}},
            audio_frame(b"\x01\x02"),
            {"serverContent": {"outputTranscription": {"text": "வணக்கம்"}}},
            {"usageMetadata": USAGE, "serverContent": {"turnComplete": True}},
        ]
    )
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        await session.end_of_speech()  # turn 1
        events = [e async for e in session.events()]

    assert [type(e) for e in events] == [AudioOut, Transcript, TurnComplete]
    assert all(e.turn_id == 1 for e in events)
    assert events[0].pcm == b"\x01\x02"
    assert events[1].text == "வணக்கம்"


async def test_turn_complete_reports_thinking_tokens(config):
    sock = FakeSocket(
        [
            {"setupComplete": {}},
            {"usageMetadata": USAGE, "serverContent": {"turnComplete": True}},
        ]
    )
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        await session.end_of_speech()
        done = [e async for e in session.events()][-1]

    assert isinstance(done, TurnComplete)
    # 90 thinking tokens that totalTokenCount does not mention.
    assert done.usage.thoughts == 90
    assert done.usage.billable_output == 103 + 90


async def test_interruption_is_surfaced_so_playback_can_be_flushed(config):
    sock = FakeSocket(
        [
            {"setupComplete": {}},
            audio_frame(b"stale"),
            {"serverContent": {"interrupted": True}},
        ]
    )
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        await session.end_of_speech()
        events = [e async for e in session.events()]

    assert isinstance(events[-1], TurnInterrupted)


async def test_first_audio_latency_is_measured_from_end_of_speech(config):
    ticks = iter([100.0, 100.4, 100.9])  # end_of_speech, first audio, unused
    sock = FakeSocket(
        [
            {"setupComplete": {}},
            audio_frame(b"\x00"),
            {"usageMetadata": USAGE, "serverContent": {"turnComplete": True}},
        ]
    )
    session = GeminiLiveSession(
        config, connect=connector(sock), clock=lambda: next(ticks)
    )
    async with session:
        await session.end_of_speech()
        done = [e async for e in session.events()][-1]

    assert done.first_audio_ms == pytest.approx(400.0)


async def test_send_audio_uses_the_configured_capture_rate(config):
    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        await session.send_audio(b"\x00\x01")
    audio = sock.sent[-1]["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(audio["data"]) == b"\x00\x01"


async def test_sending_before_connecting_fails_clearly(config):
    with pytest.raises(RuntimeError, match="not connected"):
        await GeminiLiveSession(config).send_audio(b"\x00")


async def test_the_agent_greets_first_with_the_exact_wording(config):
    """A call that answers and then waits in silence sounds like a dead line."""
    from src.agent.persona import GREETING

    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        await session.greet(GREETING)

    sent = sock.sent[-1]["clientContent"]
    assert sent["turnComplete"] is True
    text = sent["turns"][0]["parts"][0]["text"]
    # Pinned, not paraphrased: a greeting that varies run to run is a sample.
    assert GREETING in text
    # The system instruction caps replies at 15 words and defaults to Tamil.
    # The greeting is ~13 words of Tanglish, close enough to the cap that the
    # model shortened it - which is why it kept cutting off. The exemption has
    # to be in the instruction, not just implied by "exactly".
    assert "ignore your reply length limit" in text
    assert "do not translate it" in text.lower()
    assert "every word of it" in text


async def test_the_greeting_takes_a_turn_id_of_its_own(config):
    """Otherwise a later interruption would discard the greeting's audio."""
    from src.agent.persona import GREETING

    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        assert session.turn_id == 0
        await session.greet(GREETING)
        assert session.turn_id == 1


class RefusingSocket(FakeSocket):
    """Closes the connection the way the server does on a setup it rejects."""

    def __init__(self, refuse_thinking: bool) -> None:
        super().__init__([{"setupComplete": {}}])
        self.refuse_thinking = refuse_thinking

    async def send(self, data: str) -> None:
        import json as _json

        from src.voice.gemini_live import _ClosedSocket

        payload = _json.loads(data)
        wants_thinking = "thinkingConfig" in (
            payload.get("setup", {}).get("generationConfig", {})
        )
        if self.refuse_thinking and wants_thinking:
            raise _ClosedSocket
        await super().send(data)


async def test_a_refused_thinking_config_reconnects_without_it(config):
    """It was accepted, measured to work, and then refused without notice.

    A preview model is entitled to do that. Depending on the capability turned
    every call into a failed setup and a dead line, so it is attempted and not
    relied on.
    """
    sockets = [
        RefusingSocket(refuse_thinking=True),
        RefusingSocket(refuse_thinking=True),
    ]
    opened = []

    async def connect(_url):
        socket = sockets.pop(0)
        opened.append(socket)
        return socket

    async with GeminiLiveSession(config, connect=connect) as session:
        assert session.thinking_disabled is False, "the fallback was not recorded"

    assert len(opened) == 2, "it did not reconnect"
    # The retry went out without the field the server refused.
    retry = opened[1].sent[0]["setup"]["generationConfig"]
    assert "thinkingConfig" not in retry


async def test_thinking_config_is_still_sent_when_the_model_accepts_it(config):
    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        assert session.thinking_disabled is True

    generation = sock.sent[0]["setup"]["generationConfig"]
    assert generation["thinkingConfig"]["thinkingBudget"] == 0


async def test_declared_tools_reach_the_setup_message(config):
    """They silently did not, and the model answered from nothing instead.

    Without `tools` in setup the model cannot call anything, so it falls back
    on the persona - which at the time still said it had no catalogue, so it
    politely offered to check and never did.
    """
    from src.agent.tools.declarations import tool_config

    sock = FakeSocket([{"setupComplete": {}}])
    async with GeminiLiveSession(config, connect=connector(sock), tools=tool_config()):
        pass

    setup = sock.sent[0]["setup"]
    assert "tools" in setup
    names = [f["name"] for f in setup["tools"][0]["functionDeclarations"]]
    assert names == ["search_products", "product_details"]


async def test_a_tool_call_is_executed_and_answered_on_the_same_socket(config):
    asked = []

    async def handler(name, args):
        asked.append((name, args))
        return {"total_matching": 8, "products": [{"name": "Vivo Y200 Pro 5G"}]}

    sock = FakeSocket(
        [
            {"setupComplete": {}},
            {
                "toolCall": {
                    "functionCalls": [
                        {
                            "id": "c1",
                            "name": "search_products",
                            "args": {"category": "phone", "max_price": 30000},
                        }
                    ]
                }
            },
            {"serverContent": {"turnComplete": True}},
        ]
    )
    session = GeminiLiveSession(
        config,
        connect=connector(sock),
        tools={"functionDeclarations": []},
        on_tool_call=handler,
    )
    async with session:
        [e async for e in session.events()]

    assert asked == [("search_products", {"category": "phone", "max_price": 30000})]
    answer = sock.sent[-1]["toolResponse"]["functionResponses"][0]
    assert answer["id"] == "c1"
    assert answer["response"]["total_matching"] == 8


async def test_a_failing_tool_is_still_answered(config):
    """Leaving a toolCall unanswered strands the model mid-turn."""

    async def explodes(name, args):
        raise RuntimeError("upstream on fire")

    sock = FakeSocket(
        [
            {"setupComplete": {}},
            {
                "toolCall": {
                    "functionCalls": [
                        {"id": "c9", "name": "search_products", "args": {}}
                    ]
                }
            },
            {"serverContent": {"turnComplete": True}},
        ]
    )
    session = GeminiLiveSession(
        config,
        connect=connector(sock),
        tools={"functionDeclarations": []},
        on_tool_call=explodes,
    )
    async with session:
        [e async for e in session.events()]

    answer = sock.sent[-1]["toolResponse"]["functionResponses"][0]
    assert answer["response"] == {"error": "tool_failed"}
