"""The browser transport is the second implementation of the seam.

If these pass while the ALSA ones also pass, the seam is carrying two very
different transports unchanged - which is the evidence Phase 2 is relying on.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.voice.transport import AudioSink, AudioSource
from src.voice.web_transport import BrowserSink, BrowserSource


class RecordingSocket:
    def __init__(self) -> None:
        self.binary: list[bytes] = []
        self.json: list[dict] = []

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def send_json(self, data: dict) -> None:
        self.json.append(data)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return load("development")


def test_browser_transports_satisfy_the_same_protocols_as_the_local_ones():
    assert isinstance(BrowserSource(16000), AudioSource)
    assert isinstance(BrowserSink(RecordingSocket(), 24000), AudioSink)


async def test_offered_audio_comes_out_of_frames_in_order():
    source = BrowserSource(16000)
    for pcm in (b"one", b"two", b"three"):
        source.offer(pcm)
    await source.close()
    assert [f async for f in source.frames()] == [b"one", b"two", b"three"]


async def test_a_full_queue_drops_the_oldest_audio_rather_than_growing():
    """A stalled model must not leave the caller talking to the past."""
    source = BrowserSource(16000, max_queued_frames=3)
    for pcm in (b"1", b"2", b"3", b"4", b"5"):
        source.offer(pcm)
    await source.close()

    frames = [f async for f in source.frames()]
    assert len(frames) <= 3
    assert b"5" in frames, "the newest audio was dropped"
    assert b"1" not in frames, "the oldest audio was kept"


async def test_offer_never_blocks_the_socket_reader():
    source = BrowserSource(16000, max_queued_frames=2)
    await asyncio.wait_for(
        asyncio.to_thread(lambda: [source.offer(b"x") for _ in range(50)]), 2
    )


async def test_sink_sends_audio_as_binary_and_flush_as_a_message():
    socket = RecordingSocket()
    sink = BrowserSink(socket, 24000)
    await sink.write(b"\x01\x02")
    await sink.flush()

    assert socket.binary == [b"\x01\x02"]
    # Only the page can discard what is already in its playback queue.
    assert socket.json == [{"type": "flush"}]


async def test_a_closed_sink_stops_sending_rather_than_raising():
    socket = RecordingSocket()
    sink = BrowserSink(socket, 24000)
    await sink.close()
    await sink.write(b"late")
    await sink.flush()
    assert socket.binary == [] and socket.json == []


def test_the_config_endpoint_never_exposes_the_key(config):
    from src.web.server import create_app

    with TestClient(create_app(config)) as client:
        body = client.get("/api/config").json()

    assert "test-key-not-real" not in str(body)
    assert "gemini_api_key" not in body
    # What the page legitimately needs to set up audio.
    assert body["capture_sample_rate"] == 16000
    assert body["thinking_budget"] == 0


def test_the_page_is_served(config):
    from src.web.server import create_app

    with TestClient(create_app(config)) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "Poorvika voice agent" in page.text
