"""Caller speech has to reach Grok without the test typing it in.

The setup field and the message handler are the production ones. The test
feeds a fake socket, then the same `remember` the voice call uses. It never
calls `add_caller`.
"""

from __future__ import annotations

import pytest

from src.config import load
from src.memory.grok import GrokCompletion
from src.memory.summariser import CallMemory
from src.voice.gemini_live import GeminiLiveSession
from src.voice.session import Conversation
from src.web.services.voice_call import remember
from tests.fakes import FakeSink, FakeSource
from tests.test_gemini_live import FakeSocket, connector


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return load("development")


class RecordingGrok:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.model = "grok-4.3"
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> GrokCompletion:
        self.calls += 1
        self.seen.append(messages[-1]["content"])
        return GrokCompletion("Language: Tanglish", 10, 0, 4, self.model)


async def test_caller_partials_reach_the_summariser_from_the_live_socket(config):
    """Shipped setup, fake socket, production remember()."""
    sock = FakeSocket(
        [
            {"setupComplete": {}},
            {"serverContent": {"inputTranscription": {"text": "20,000 "}}},
            {"serverContent": {"inputTranscription": {"text": "camera phone"}}},
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/pcm",
                                    "data": "AQI=",
                                }
                            }
                        ]
                    }
                }
            },
            {"serverContent": {"outputTranscription": {"text": "Poco."}}},
            {"serverContent": {"turnComplete": True}},
        ]
    )
    model = RecordingGrok()
    memory = CallMemory(model, every=1)
    updates: list[dict] = []

    async def on_update(payload: dict) -> None:
        updates.append(payload)
        remember(memory, payload)

    async with GeminiLiveSession(config, connect=connector(sock)) as session:
        setup = sock.sent[0]["setup"]
        # The field the Live websocket actually reads, on this socket and
        # on every later one: rotation builds a new GeminiLiveSession.
        assert setup["inputAudioTranscription"] == {}
        assert setup["outputAudioTranscription"] == {}

        spare = FakeSocket([{"setupComplete": {}}])
        async with GeminiLiveSession(config, connect=connector(spare)):
            assert spare.sent[0]["setup"]["inputAudioTranscription"] == {}

        conversation = Conversation(
            session, FakeSource([]), FakeSink(), on_update=on_update
        )
        await conversation._pump_model()

    # Audio was delivered. Grok was not asked yet: the summary is after
    # the reply, not on the way to the first byte.
    assert model.calls == 0
    assert any(item.get("type") == "caller_transcript" for item in updates)

    # The reply has landed. Counting the caller turn is the cadence, not
    # the text. The text came from remember(), above.
    assert memory.caller_reply_finished() is True
    await memory.summarise()

    assert model.calls == 1
    transcript = model.seen[0]
    assert "caller: 20,000 camera phone" in transcript
    assert transcript.count("caller:") == 1
    assert "agent: Poco." in transcript


def test_config_fixture_is_the_shipped_live_model(config):
    assert config.gemini_live_model == "gemini-3.8-live"
