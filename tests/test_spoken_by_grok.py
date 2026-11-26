"""Live hears and thinks; Grok speaks.

Not the cascade. The cascade replaced the EAR, and that is what killed it -
xAI's STT cannot begin until the caller stops talking, so a turn paid for
the transcription before any thinking started. Here the ear and the brain
are the same Live session, which hears the caller's audio while they are
still speaking, and only the last step changes.

So the tests are about the seam: text goes to the mouth as it streams, the
mouth's audio comes back as this turn's audio, and the turn ends ONCE.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from src.voice.events import AudioOut, CallerTranscript, Event, Transcript, TurnComplete
from src.voice.spoken_by_grok import SpokenByGrok
from src.voice.usage import TurnUsage

SAID = TurnUsage(response_text=40)


class FakeLive:
    """A Live session answering in text."""

    def __init__(self, *events: Event) -> None:
        self._events = list(events)
        self.turn_id = 3
        self.sent: list[bytes] = []
        self.ends = 0

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def end_of_speech(self) -> None:
        self.ends += 1

    async def events(self) -> AsyncIterator[Event]:
        for event in self._events:
            yield event


class FakeMouth:
    """Records what it was fed; speaks it back as one lump of audio."""

    def __init__(self, pcm: bytes = b"\x01\x02") -> None:
        self.fed: list[str] = []
        self.finished = 0
        self.characters = 0
        self._pcm = pcm
        self._out: asyncio.Queue[Event] = asyncio.Queue()

    async def feed(self, text: str) -> None:
        self.fed.append(text)
        self.characters += len(text)

    async def done(self) -> None:
        self.finished += 1
        if self._pcm:
            await self._out.put(AudioOut(turn_id=0, pcm=self._pcm))
        await self._out.put(
            TurnComplete(turn_id=0, usage=TurnUsage(), first_audio_ms=None)
        )

    def events(self) -> AsyncIterator[Event]:
        return self._drain()

    async def _drain(self) -> AsyncIterator[Event]:
        while True:
            yield await self._out.get()


async def run(live: FakeLive, mouth: FakeMouth) -> list[Any]:
    return [event async for event in SpokenByGrok(live, mouth).events()]


class TestTheSeam:
    async def test_the_text_is_fed_as_it_streams(self):
        """Not gathered and sent at the end. The model writes its first
        clause long before its last, and the mouth can already be saying
        it - waiting would add the whole generation to the mouth's 450 ms."""
        live = FakeLive(
            Transcript(turn_id=3, text="Poco M8 5G, "),
            Transcript(turn_id=3, text="19,999 rupees."),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        mouth = FakeMouth()
        await run(live, mouth)

        assert mouth.fed == ["Poco M8 5G, ", "19,999 rupees."]
        assert mouth.finished == 1

    async def test_the_mouth_s_audio_comes_back_as_this_turn_s(self):
        live = FakeLive(
            Transcript(turn_id=3, text="Vanakkam"),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        out = await run(live, FakeMouth(b"\x07\x08"))

        audio = [e for e in out if isinstance(e, AudioOut)]
        assert [a.pcm for a in audio] == [b"\x07\x08"]
        assert audio[0].turn_id == 3, "audio tagged with the mouth's own id"

    async def test_the_turn_ends_exactly_once(self):
        """The mouth completes too, and that one is swallowed. Two would
        tell `Conversation` the turn ended twice and open the microphone a
        turn early, while the agent was still speaking."""
        live = FakeLive(
            Transcript(turn_id=3, text="Vanakkam"),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        out = await run(live, FakeMouth())

        assert len([e for e in out if isinstance(e, TurnComplete)]) == 1

    async def test_the_audio_arrives_before_the_turn_ends(self):
        """`Conversation` releases the floor on TurnComplete. Audio after it
        belongs to a turn it has already closed."""
        live = FakeLive(
            Transcript(turn_id=3, text="Vanakkam"),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        out = await run(live, FakeMouth())

        kinds = [type(e).__name__ for e in out]
        assert kinds.index("AudioOut") < kinds.index("TurnComplete")

    async def test_the_live_usage_is_what_is_billed(self):
        """The mouth is billed in characters, priced separately. Replacing
        the Live usage with the mouth's empty one would lose the turn."""
        live = FakeLive(
            Transcript(turn_id=3, text="Vanakkam"),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        out = await run(live, FakeMouth())

        done = [e for e in out if isinstance(e, TurnComplete)][0]
        assert done.usage == SAID


class TestWhatPassesThrough:
    async def test_the_page_still_sees_what_was_said(self):
        live = FakeLive(
            Transcript(turn_id=3, text="Vanakkam"),
            TurnComplete(turn_id=3, usage=SAID, first_audio_ms=300.0),
        )
        out = await run(live, FakeMouth())

        assert any(isinstance(e, Transcript) for e in out)

    async def test_the_caller_s_own_transcript_is_untouched(self):
        heard = CallerTranscript(turn_id=3, text="enakku phone venum")
        live = FakeLive(heard, TurnComplete(turn_id=3, usage=SAID, first_audio_ms=None))
        out = await run(live, FakeMouth(b""))

        assert heard in out

    async def test_the_caller_s_audio_reaches_the_live_session(self):
        live = FakeLive()
        wrapper = SpokenByGrok(live, FakeMouth())
        await wrapper.send_audio(b"frame")
        await wrapper.end_of_speech()

        assert live.sent == [b"frame"] and live.ends == 1
