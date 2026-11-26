"""The mouth that is not Gemini.

The cascade asks a mouth for two things - `say` and a stream of events -
and releases the caller's microphone on a TurnComplete. So the whole of
what matters here is that a turn ALWAYS ends: a mouth that goes quiet
without completing leaves the gate shut and the caller talking into
nothing, which is a trace this project has already collected once.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from src.voice.events import AudioOut, TurnComplete
from src.voice.grok_tts import GrokMouth, GrokMouthError
from src.voice.usage import TurnUsage


def frame(pcm: bytes) -> str:
    return json.dumps({"type": "audio.delta", "delta": base64.b64encode(pcm).decode()})


class FakeSocket:
    """Replays server frames; records what was sent."""

    def __init__(self, *frames: str) -> None:
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.url = ""
        self.headers: dict[str, str] = {}

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            # A real socket blocks here rather than ending the call.
            await asyncio.Event().wait()
        return self._frames.pop(0)


def connector(sock: FakeSocket):
    async def connect(url: str, **kwargs: Any) -> FakeSocket:
        sock.url = url
        sock.headers = kwargs.get("additional_headers") or {}
        return sock

    return connect


async def collect(mouth: GrokMouth) -> list[Any]:
    """Everything up to and including the first TurnComplete."""
    out = []
    async for event in mouth.events():
        out.append(event)
        if isinstance(event, TurnComplete):
            return out
    return out  # pragma: no cover


class TestTheRequest:
    async def test_it_asks_for_raw_pcm_at_the_playback_rate(self):
        """The browser plays 24 kHz PCM. Anything else arrives as noise."""
        sock = FakeSocket()
        async with GrokMouth("k", rate=24000, connect=connector(sock)) as mouth:
            assert "codec=pcm" in mouth.url
            assert "sample_rate=24000" in mouth.url

    async def test_the_key_travels_in_a_header(self):
        sock = FakeSocket()
        async with GrokMouth("secret-key", connect=connector(sock)):
            pass
        assert sock.headers["Authorization"] == "Bearer secret-key"
        assert "secret-key" not in sock.url

    async def test_saying_something_sends_the_text_and_closes_it(self):
        sock = FakeSocket()
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("Vanakkam")
        assert sock.sent[0] == {"type": "text.delta", "delta": "Vanakkam"}
        assert sock.sent[1] == {"type": "text.done"}

    async def test_an_empty_key_is_refused_before_a_socket_is_opened(self):
        with pytest.raises(GrokMouthError):
            async with GrokMouth("", connect=connector(FakeSocket())):
                pass  # pragma: no cover


class TestTheTurnAlwaysEnds:
    """`Conversation` opens the microphone on TurnComplete and on nothing
    else. Every path out of this class has to produce one."""

    async def test_audio_then_a_completed_turn(self):
        sock = FakeSocket(frame(b"\x01\x02"), json.dumps({"type": "audio.done"}))
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("Vanakkam")
            out = await asyncio.wait_for(collect(mouth), 2.0)

        assert isinstance(out[0], AudioOut)
        assert out[0].pcm == b"\x01\x02"
        assert isinstance(out[-1], TurnComplete)

    async def test_an_error_ends_the_turn_rather_than_raising(self):
        """A mouth that refuses must not leave the caller behind a shut gate."""
        sock = FakeSocket(json.dumps({"type": "error", "message": "nope"}))
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("Vanakkam")
            out = await asyncio.wait_for(collect(mouth), 2.0)

        assert isinstance(out[-1], TurnComplete)

    async def test_nothing_to_say_still_ends_the_turn(self):
        sock = FakeSocket()
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("   ")
            out = await asyncio.wait_for(collect(mouth), 2.0)

        assert isinstance(out[-1], TurnComplete)
        assert sock.sent == [], "an empty line must not be sent to the API"

    async def test_the_turn_is_billed_in_characters_not_tokens(self):
        """xAI charges per character. A token figure here would be wrong by
        about four times, in the panel built to be checked against a bill."""
        sock = FakeSocket(json.dumps({"type": "audio.done"}))
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("Vanakkam")
            out = await asyncio.wait_for(collect(mouth), 2.0)

        assert out[-1].usage == TurnUsage()
        assert mouth.characters == len("Vanakkam")


class TestWhatItDoesNotDo:
    async def test_the_keepalive_is_a_no_op(self):
        """The cascade sends its mouth silence every few seconds because a
        Live session is closed for inactivity. A TTS socket is not."""
        sock = FakeSocket()
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.send_audio(b"\x00" * 640)
        assert sock.sent == []

    async def test_characters_add_up_across_a_call(self):
        sock = FakeSocket()
        async with GrokMouth("k", connect=connector(sock)) as mouth:
            await mouth.say("onnu")
            await mouth.say("rendu")
        assert mouth.characters == len("onnu") + len("rendu")
