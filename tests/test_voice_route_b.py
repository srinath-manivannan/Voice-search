"""Voice Call stays put when live voice is off, and Route B speaks over /ws."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import load
from src.voice.cascade import CascadeSession
from src.web.app import create_app
from tests.test_cascade import RATE, FakeBrain, FakeEar, FakeMouth


def _settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "full")
    monkeypatch.setenv("LIVE_VOICE", "1")
    monkeypatch.setenv("VOICE_ROUTE_B", "1")
    monkeypatch.setenv("VOICE_ROUTE_A", "0")
    return load("development")


def test_route_b_without_route_a_serves_a_cascade_turn(monkeypatch):
    monkeypatch.setattr(
        "src.web.services.voice_call.load_recording", lambda *_args, **_kwargs: None
    )
    app = create_app(_settings(monkeypatch))
    mouth = FakeMouth()

    async def say(text: str) -> None:
        mouth.said.append(text)
        mouth.speak_back(b"\x01\x00" * 40)

    mouth.say = say  # type: ignore[method-assign]

    def factory(_tools, _listener):
        return CascadeSession(
            FakeEar("a phone under twenty thousand"),
            FakeBrain("Two options are on screen."),
            mouth,
            rate=RATE,
            ear_timeout_s=1.0,
        )

    app.state.cascade_factory = factory

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert _next_text(ws, "ready")
            # The greeting holds the floor. Speech sent during it is dropped.
            _next_text(ws, '"type":"turn"')
            # A steady tone. The stock speech helper dips under the VAD
            # threshold every few frames, so a turn is never opened.
            loud = (8000).to_bytes(2, "little", signed=True) * int(0.5 * RATE)
            for start in range(0, len(loud), 640):
                ws.send_bytes(loud[start : start + 640])
            for _ in range(40):
                ws.send_bytes(bytes(640))
            heard = _next_text(ws, "Two options are on screen")
    assert "Two options are on screen" in heard
    assert any("two options" in line.lower() for line in mouth.said)


def _next_text(ws, needle: str, limit: int = 80) -> str:  # noqa: ANN001
    for _ in range(limit):
        message = ws.receive()
        text = str(message.get("text") or "")
        if needle in text:
            return text
    raise AssertionError(f"never saw {needle}")


def test_gemini_only_uses_no_other_speech_or_memory_provider(monkeypatch):
    from types import SimpleNamespace

    from src.voice.gemini_live import GeminiLiveSession
    from src.voice.gemini_stt import GeminiEar
    from src.web.services.voice_call import VoiceCall

    for key in ("GROK_MOUTH", "GROK_MEMORY", "WHISPER_COMPARE", "VOICE_GEMINI_ONLY"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("XAI_API_KEY", "offline")
    monkeypatch.setenv("GROQ_API_KEY", "offline")
    call = VoiceCall(SimpleNamespace(), _settings(monkeypatch), None)
    assert isinstance(call._ear(), GeminiEar)
    assert isinstance(call._mouth(), GeminiLiveSession)
    assert call._memory is None and call._shadow is None


async def test_gemini_transcription_charge_is_counted_even_for_empty_text(monkeypatch):
    import wave
    from io import BytesIO

    from src.guided.listen import Ear
    from src.voice.cost import TRANSCRIPTION
    from src.voice.gemini_stt import GeminiEar

    async def transcribe(self, audio, language):
        assert language == "ta"
        with wave.open(BytesIO(audio)) as wav:
            assert wav.getframerate() == RATE
            assert wav.getnframes() == RATE
        return "", 0.0004

    monkeypatch.setattr(Ear, "transcribe", transcribe)
    cascade = CascadeSession(
        GeminiEar("offline", "test-model", "ta"), FakeBrain(), FakeMouth(), rate=RATE
    )
    assert await cascade._transcribe(bytes(RATE * 2)) is None
    usage = cascade.usage
    report = usage.breakdown(89, 0.18, 1)
    assert report.line(TRANSCRIPTION).usd == usage.total_usd == 0.0004
    assert report.total_usd == usage.total_usd
