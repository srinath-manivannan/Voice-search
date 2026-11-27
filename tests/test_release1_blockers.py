"""Release 1 blocker regression suite: retry, refresh, startup, multi-worker fanout."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.chat.copy import SERVICE_ERROR
from src.chat.gemini_text import ChatTurn
from src.config import load
from src.web.app import create_app
from src.web.services.chat_v2 import SocketFanout
from src.web.services.conversation_store import (
    HOLD_SECONDS,
    REAP_EVERY_SECONDS,
    STALE_CLAIM_SECONDS,
    Caps,
    Conversation,
    MemoryStore,
    RedisStore,
)

HOST = "https://www.shop.example"
MESSAGE = "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a10"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODE", "widget")
    monkeypatch.setenv("WIDGET_HOST_ORIGINS", HOST)
    monkeypatch.setenv("WIDGET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("CONVERSATION_TOKEN_SECRET", "test-conversation-secret")
    monkeypatch.setenv("WIDGET_DEMO_CATALOGUE", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load("development")


def _hello(token: str | None = None, last_seq: int = 0) -> dict[str, Any]:
    return {
        "type": "hello",
        "v": 2,
        "token": token,
        "last_seq": last_seq,
        "context": {
            "host_origin": HOST,
            "host_session": "hs_blocker_1",
            "pincode": "600040",
        },
    }


def _until(ws: Any, kind: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for _ in range(12):
        message = ws.receive_json()
        seen.append(message)
        if message.get("type") == kind:
            return seen
    raise AssertionError(seen)


class FlakyModel:
    """Fails on its first generation, succeeds on the retry."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, history: Any, temperature: float = 0.7):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("upstream 503")
        turn = ChatTurn()
        turn.usage = {"promptTokenCount": 1200, "candidatesTokenCount": 30}
        turn.text = "Recovered on the retry."
        yield turn.text, turn


def test_failed_message_is_not_stored_and_retries_with_the_same_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(monkeypatch))
    flaky = FlakyModel()
    app.state.widget_model = flaky
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(_hello())
            welcome = ws.receive_json()
            token = welcome["token"]
            body = {
                "type": "user_message",
                "message_id": MESSAGE,
                "text": "What is your return policy?",
            }
            ws.send_json(body)
            first = _until(ws, "error")
            err = first[-1]
            assert err["type"] == "error"
            assert err["code"] == "upstream"
            assert err["message_id"] == MESSAGE
            assert err["retryable"] is True
            assert SERVICE_ERROR in err["message"]
            assert not any(item["type"] == "final" for item in first)

            # History must not contain a dead turn before the retry succeeds.
            mid_hist = client.get(
                "/api/v1/widget/history",
                headers={"Authorization": f"Bearer {token}", "X-Sa-Host": HOST},
            ).json()
            assert mid_hist["messages"] == []

            # Retry with the exact same message_id: succeeds and charges once.
            ws.send_json(body)
            retried = _until(ws, "done")
            final = next(item for item in retried if item["type"] == "final")
            done = retried[-1]
            assert final["display"]["text"] == "Recovered on the retry."
            assert done["spend"]["conversation_inr"] > 0
            assert flaky.calls == 2

            # Sending the same id a third time replays the completed answer without calling the model.
            ws.send_json(body)
            replayed = _until(ws, "done")
            assert [item["type"] for item in replayed] == ["final", "done"]
            assert flaky.calls == 2

        end_hist = client.get(
            "/api/v1/widget/history",
            headers={"Authorization": f"Bearer {token}", "X-Sa-Host": HOST},
        ).json()
        assert len(end_hist["messages"]) == 2
        assert [m["role"] for m in end_hist["messages"]] == ["user", "assistant"]
        assert all(m["message_id"] == MESSAGE for m in end_hist["messages"])
        assert end_hist["last_seq"] > 0


def test_history_returns_last_seq_so_reconnect_does_not_replay_drawn_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(monkeypatch, WIDGET_STUB_MODEL="1"))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(_hello())
            token = ws.receive_json()["token"]
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "What is your return policy?",
                }
            )
            _until(ws, "done")
        hist = client.get(
            "/api/v1/widget/history",
            headers={"Authorization": f"Bearer {token}", "X-Sa-Host": HOST},
        ).json()
        assert hist["last_seq"] >= 3
        assert len(hist["messages"]) == 2
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(_hello(hist["token"], last_seq=hist["last_seq"]))
            welcome = ws.receive_json()
            assert welcome["replay"] == []


def test_startup_refuses_production_without_catalogue_or_token_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("TYPESENSE_API_KEY", "CATALOGUE_TOKEN", "WIDGET_DEMO_CATALOGUE"):
        monkeypatch.delenv(key, raising=False)
    cfg = _settings(monkeypatch, WIDGET_DEMO_CATALOGUE="0")
    from dataclasses import replace

    no_cat = replace(cfg, env="main", catalogue_token="", widget_demo_catalogue=False)
    with pytest.raises(RuntimeError, match="Widget catalogue is not configured"):
        create_app(no_cat)

    no_secret_main = replace(
        cfg,
        env="main",
        widget_demo_catalogue=True,
        conversation_token_secret="",
    )
    with pytest.raises(RuntimeError, match="CONVERSATION_TOKEN_SECRET"):
        create_app(no_secret_main)

    no_secret_redis = replace(
        cfg,
        redis_url="redis://127.0.0.1:6379/0",
        conversation_token_secret="",
    )
    with pytest.raises(RuntimeError, match="CONVERSATION_TOKEN_SECRET"):
        create_app(no_secret_redis)

    no_public_origin = replace(
        cfg, env="main", widget_demo_catalogue=True, widget_public_origin=""
    )
    with pytest.raises(RuntimeError, match="WIDGET_PUBLIC_ORIGIN"):
        create_app(no_public_origin)

    no_host_origins = replace(
        cfg, env="main", widget_demo_catalogue=True, widget_host_origins=()
    )
    with pytest.raises(RuntimeError, match="WIDGET_HOST_ORIGINS"):
        create_app(no_host_origins)


async def test_stale_in_progress_claim_recovers_with_incremented_attempt() -> None:
    store = MemoryStore(Caps(100, 300, 30_000), "secret")
    first = await store.claim_message("c1", MESSAGE, "hello")
    assert first.status == "accepted" and first.attempt == 0
    store._messages[("c1", MESSAGE)]["started"] -= STALE_CLAIM_SECONDS + 5
    recovered = await store.claim_message("c1", MESSAGE, "hello")
    assert recovered.status == "accepted" and recovered.attempt == 1


async def test_redis_claim_failure_is_reported_as_retryable_store_unavailable() -> None:
    class UnavailableRedis(RedisStore):
        async def _safe(self, *parts: str) -> Any:
            return None

    store = UnavailableRedis(
        "redis://127.0.0.1:6379/0", Caps(100, 300, 30_000), "secret"
    )
    claim = await store.claim_message("c1", MESSAGE, "hello")
    assert claim.status == "unavailable"


async def test_redis_reserve_sweeps_stale_holds_and_publishes_live_events() -> None:
    calls: list[tuple[str, ...]] = []

    class FakeRedis(RedisStore):
        async def _safe(self, *parts: str) -> Any:
            calls.append(parts)
            if parts[0] == "EVAL" and "SMEMBERS" in parts[1]:
                return 1
            if parts[0] == "EVAL" and "PUBLISH" in parts[1]:
                return 4
            if parts[0] == "EVAL":
                return "1|ok"
            return "OK"

    store = FakeRedis("redis://127.0.0.1:6379/0", Caps(100, 300, 30_000), "secret")
    conv = Conversation(
        id="c-live",
        host_origin=HOST,
        host_session="hs",
        created=0,
        updated=0,
        absolute_exp=10**12,
    )
    res = await store.reserve(conv, "client", "rid-1", 10)
    assert res.ok
    assert sum(1 for c in calls if c[0] == "EVAL" and "SMEMBERS" in c[1]) == 1

    # A second reserve right away does not reap again until REAP_EVERY_SECONDS elapses.
    await store.reserve(conv, "client", "rid-2", 10)
    assert sum(1 for c in calls if c[0] == "EVAL" and "SMEMBERS" in c[1]) == 1
    store._last_reap -= REAP_EVERY_SECONDS + 1
    await store.reserve(conv, "client", "rid-3", 10)
    assert sum(1 for c in calls if c[0] == "EVAL" and "SMEMBERS" in c[1]) == 2
    assert HOLD_SECONDS == 120

    seq = await store.append_event("c-live", {"type": "final", "message_id": MESSAGE})
    assert seq == 4
    publishes = [c for c in calls if c[0] == "EVAL" and "PUBLISH" in c[1]]
    assert len(publishes) == 1
    assert publishes[0][5] == "conv:c-live:live"


async def test_socket_fanout_deduplicates_remote_pubsub_echo() -> None:
    class DummySocket:
        def __init__(self) -> None:
            self.received: list[dict[str, Any]] = []

        async def send_json(self, body: dict[str, Any]) -> None:
            self.received.append(body)

    gate = asyncio.Event()
    delivered = asyncio.Event()

    class PubSubStore(MemoryStore):
        async def subscribe_live(self, conv_id: str, fanout: Any) -> None:
            await gate.wait()
            await fanout.send(conv_id, {"type": "ack", "seq": 1})
            await fanout.send(conv_id, {"type": "final", "seq": 2})
            delivered.set()

    store = PubSubStore(Caps(100, 300, 30_000), "secret")
    fanout = SocketFanout()
    sock = DummySocket()
    fanout.add("c1", sock, store)  # type: ignore[arg-type]
    # Local worker already emitted seq 1; remote worker emits seq 2.
    await fanout.send("c1", {"type": "ack", "seq": 1})
    gate.set()
    await asyncio.wait_for(delivered.wait(), 1)
    await fanout.send("c1", {"type": "final", "seq": 2})
    fanout.discard("c1", sock)  # type: ignore[arg-type]
    assert [m["seq"] for m in sock.received] == [1, 2]


async def test_sequence_keeps_advancing_when_replay_is_trimmed() -> None:
    store = MemoryStore(Caps(100, 300, 30_000), "secret")
    for _ in range(205):
        await store.append_event("c1", {"type": "delta"})
    assert await store.last_seq("c1") == 205
    assert [event["seq"] for event in await store.events_after("c1", 201)] == [
        202,
        203,
        204,
        205,
    ]


async def test_voice_caller_speech_during_greeting_is_forwarded_when_greeting_ends() -> (
    None
):
    import struct
    from src.voice.events import AudioOut, TurnComplete
    from src.voice.opening import SpeaksTheOpening
    from src.voice.session import Conversation
    from src.voice.usage import TurnUsage
    from src.voice.vad import EnergyVad
    from tests.fakes import FakeSink, FakeSource
    from tests.test_session import SILENCE, SPEECH, StubModel

    greeting_pcm = struct.pack("<960h", *([1200, -1200] * 480))
    inner = StubModel(
        [
            AudioOut(turn_id=1, pcm=b"\x01\x02" * 100),
            TurnComplete(
                turn_id=1, usage=TurnUsage(response_audio=50), first_audio_ms=100.0
            ),
        ],
        keep_open=True,
    )
    # Caller speaks while the greeting is playing, then goes silent.
    source = FakeSource([SPEECH] * 10 + [SILENCE] * 40)
    conv = Conversation(
        SpeaksTheOpening(inner, greeting_pcm, 24000, pace=0.0),
        source,
        FakeSink(),
        vad=EnergyVad(),
        agent_opens=True,
    )
    await conv.run()
    # Even though speech started during the protected greeting, end_of_speech
    # fires on release and non-zero caller PCM was flushed to the model.
    assert inner.ends == 1
    assert any(any(frame) for frame in inner.sent)


def test_widget_guide_handles_multi_shelf_categories_and_tailored_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(monkeypatch))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json(_hello())
            ws.receive_json()
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": MESSAGE,
                    "text": "Best AC for my room",
                }
            )
            ac_turn = next(
                item for item in _until(ws, "done") if item["type"] == "final"
            )
            assert "ton" in ac_turn["display"]["text"].lower()
            assert any("Ton" in chip for chip in ac_turn["suggestions"])
            ws.send_json(
                {
                    "type": "user_message",
                    "message_id": "3b0e7a52-2f7c-4c3e-9a51-0c7f4f3d9a11",
                    "text": "Check store availability",
                }
            )
            avail_turn = next(
                item for item in _until(ws, "done") if item["type"] == "final"
            )
            assert avail_turn["suggestions"]
