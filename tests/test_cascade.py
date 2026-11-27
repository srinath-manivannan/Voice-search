"""Ear, brain and mouth, as one model.

The first two tests are the ones the whole architecture rests on: no caller
audio reaches a context window, and ending a turn does not block the
microphone. Everything after them is a way for the call to survive a piece
that failed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.voice.cascade import SPEAK_ONLY, CascadeSession
from src.voice.events import (
    AudioOut,
    CallerTranscript,
    Transcript,
    TurnComplete,
    TurnInterrupted,
)
from src.voice.usage import FLASH_LITE, SessionUsage, TurnUsage
from src.voice.whisper import Heard, WhisperError

RATE = 16000


def speech(seconds: float) -> bytes:
    """Frames with something in them.

    NOT `bytes(n)`. That is digital silence, which is what the microphone
    gate substitutes while the agent speaks and which the cascade now drops
    - so a helper that returned zeroes was feeding the ear exactly the thing
    that made the first real call deaf, and passing.
    """
    samples = int(seconds * RATE)
    return b"".join(
        ((i % 2000) - 1000).to_bytes(2, "little", signed=True) for i in range(samples)
    )


class FakeEar:
    def __init__(self, *answers: str | Exception) -> None:
        self._answers = list(answers)
        self.heard: list[int] = []

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        self.heard.append(len(pcm))
        answer = self._answers.pop(0) if self._answers else "…"
        if isinstance(answer, Exception):
            raise answer
        return Heard(text=answer, seconds=1.0, billed_seconds=10.0, model="fake")


class FakeBrain:
    """A ChatSession as far as the cascade can tell."""

    def __init__(self, *replies: str | Exception, usd: float = 0.0) -> None:
        self._replies = list(replies)
        self._usd = usd
        self.asked: list[str] = []
        self.remembered: list[str] = []
        self.usage = SessionUsage(rates=FLASH_LITE)

    def remember(self, context: str) -> None:
        self.remembered.append(context)

    async def send(self, text: str):
        self.asked.append(text)
        reply = self._replies.pop(0) if self._replies else "sari"
        if isinstance(reply, Exception):
            raise reply
        # Priced the way a real turn would be, so the cascade has a delta to
        # carry across into its own total.
        self.usage.turns.append(TurnUsage(prompt_text=1000, response_text=100))
        for word in reply.split():
            yield word + " "


class FakeMouth:
    """Emits whatever is pushed onto it, and records what it was told to say."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.kept_alive = 0
        self.opened = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self.fail: Exception | None = None

    async def __aenter__(self) -> FakeMouth:
        self.opened = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.opened = False

    async def say(self, text: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.said.append(text)

    async def send_audio(self, pcm: bytes) -> None:
        self.kept_alive += 1

    def interrupt(self, turn_id: int = 7) -> None:
        self._queue.put_nowait(TurnInterrupted(turn_id=turn_id))

    def speak_back(self, pcm: bytes, turn_id: int = 7) -> None:
        """What the real mouth would emit: audio, then a completed turn."""
        self._queue.put_nowait(AudioOut(turn_id=turn_id, pcm=pcm))
        self._queue.put_nowait(
            TurnComplete(
                turn_id=turn_id,
                usage=TurnUsage(response_audio=900),
                first_audio_ms=480.0,
            )
        )

    async def events(self):
        while True:
            yield await self._queue.get()


def build(ear: Any, brain: Any, mouth: Any) -> CascadeSession:
    return CascadeSession(ear, brain, mouth, rate=RATE, ear_timeout_s=1.0)


async def collect(session: CascadeSession, how_many: int, timeout: float = 2.0):
    out = []
    stream = session.events()

    async def pull():
        async for event in stream:
            out.append(event)
            if len(out) >= how_many:
                return

    await asyncio.wait_for(pull(), timeout)
    return out


class TestTheTwoThatMatter:
    async def test_caller_audio_never_reaches_a_model(self):
        """The line the whole cost argument rests on.

        On the Live path every frame is sent, retained as audio, and
        re-billed on every generation after. Here it goes into a buffer and
        the only thing that ever leaves is text.
        """
        ear, brain, mouth = FakeEar("venum"), FakeBrain(), FakeMouth()
        session = build(ear, brain, mouth)

        for _ in range(50):
            await session.send_audio(speech(0.02))

        assert mouth.said == []
        assert ear.heard == []
        assert brain.asked == []

    async def test_end_of_speech_does_not_wait_for_the_answer(self):
        """It is called inside the microphone pump's own loop.

        A version that awaited the ear, the brain and the mouth would stop
        reading the microphone for about 1.8 seconds and lose whatever the
        caller said next. The Live path's version sends one frame and returns.
        """
        slow = asyncio.Event()

        class SlowEar:
            async def hear(self, pcm: bytes, *, rate: int) -> Heard:
                slow.set()
                await asyncio.sleep(30)
                raise AssertionError("should not be awaited by end_of_speech")

        session = build(SlowEar(), FakeBrain(), FakeMouth())
        await session.send_audio(speech(2.0))

        await asyncio.wait_for(session.end_of_speech(), 0.5)
        await asyncio.wait_for(slow.wait(), 1.0)
        await session.close()


class TestAWholeTurn:
    async def test_audio_in_text_out_speech_back(self):
        ear = FakeEar("enakku 20000 kulla phone venum")
        brain = FakeBrain("Redmi 15C irukku")
        mouth = FakeMouth()
        session = build(ear, brain, mouth)

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        events = await collect(session, 2)

        assert events[0] == CallerTranscript(
            turn_id=1, text="enakku 20000 kulla phone venum"
        )
        assert isinstance(events[1], Transcript)
        assert events[1].text == "Redmi 15C irukku"
        # The brain was asked what the ear heard, and the mouth was handed
        # the finished reply rather than the question.
        assert brain.asked == ["enakku 20000 kulla phone venum"]
        assert mouth.said == ["Redmi 15C irukku"]

    async def test_the_mouths_audio_is_retagged_with_our_turn_id(self):
        """The mouth counts its own turns and they do not line up.

        A consumer drops audio for abandoned turns BY ID, so a mouth's id
        leaking through would silence a turn that is still being spoken.
        """
        session = build(FakeEar("venum"), FakeBrain("sari"), mouth := FakeMouth())

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        await collect(session, 2)
        mouth.speak_back(b"\x01\x02", turn_id=7)

        events = await collect(session, 2)
        audio = [e for e in events if isinstance(e, AudioOut)]
        assert audio and audio[0].turn_id == session.turn_id == 1

    async def test_both_rate_cards_reach_the_total(self):
        """Two models bill this call, and only one of them is in `turns`.

        The brain prices itself on flash-lite's card. Reporting only the
        spoken half would make the cheaper architecture look cheaper still.
        """
        brain = FakeBrain("sari")
        session = build(FakeEar("venum"), brain, mouth := FakeMouth())

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        await collect(session, 2)
        mouth.speak_back(b"\x01")
        await collect(session, 2)

        assert session.usage.side_usd == pytest.approx(brain.usage.total_usd)
        assert session.usage.side_usd > 0
        assert session.usage.total_usd > session.usage.side_usd


class TestWhenAPieceFails:
    async def test_an_ear_that_refuses_ends_the_turn(self):
        """TurnComplete releases the latch that lets the caller be heard again.

        Without one the call goes silent for good rather than for a turn.
        """
        session = build(FakeEar(WhisperError("503")), FakeBrain(), FakeMouth())

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        events = await collect(session, 1)

        assert isinstance(events[0], TurnComplete)
        assert events[0].first_audio_ms is None

    async def test_a_brain_that_blows_up_ends_the_turn(self):
        session = build(
            FakeEar("venum"), FakeBrain(RuntimeError("upstream")), FakeMouth()
        )

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        events = await collect(session, 2)

        assert isinstance(events[0], CallerTranscript)
        assert isinstance(events[1], TurnComplete)

    async def test_a_mouth_that_will_not_speak_ends_the_turn(self):
        mouth = FakeMouth()
        mouth.fail = RuntimeError("socket closed")
        session = build(FakeEar("venum"), FakeBrain("sari"), mouth)

        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        events = await collect(session, 3)

        assert isinstance(events[-1], TurnComplete)

    async def test_a_gate_that_opened_and_closed_costs_nothing(self):
        """Groq bills ten seconds a request; a stray frame is not an utterance."""
        ear = FakeEar("should not be asked")
        session = build(ear, FakeBrain(), FakeMouth())

        await session.send_audio(speech(0.1))
        await session.end_of_speech()
        events = await collect(session, 1)

        assert ear.heard == []
        assert isinstance(events[0], TurnComplete)


class TestTheRest:
    async def test_a_second_utterance_abandons_the_first_answer(self):
        """The caller moved on, so the answer in flight is about nothing."""
        started = asyncio.Event()

        class SlowEar:
            async def hear(self, pcm: bytes, *, rate: int) -> Heard:
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("cancelled")

        session = build(SlowEar(), FakeBrain(), FakeMouth())
        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        await started.wait()
        first = session._thinking

        await session.send_audio(speech(2.0))
        await session.end_of_speech()

        assert first is not None
        # `cancel` only requests it; the task is cancelled a scheduler turn
        # later, which is why this waits rather than asserting straight away.
        with pytest.raises(asyncio.CancelledError):
            await first
        assert first.cancelled()
        assert session._thinking is not first
        await session.close()

    async def test_priming_tells_the_brain_without_asking_for_an_answer(self):
        session = build(FakeEar(), brain := FakeBrain(), FakeMouth())
        await session.prime("search_products has already run for phone")
        assert brain.remembered == ["search_products has already run for phone"]
        assert brain.asked == []

    async def test_priming_with_nothing_says_nothing(self):
        session = build(FakeEar(), brain := FakeBrain(), FakeMouth())
        await session.prime("   ")
        assert brain.remembered == []

    async def test_the_greeting_is_spoken_without_anyone_having_spoken(self):
        session = build(FakeEar(), FakeBrain(), mouth := FakeMouth())
        await session.greet("Vanakkam, sollunga.")
        assert mouth.said == ["Vanakkam, sollunga."]
        assert session.turn_id == 1

    async def test_an_interrupted_mouth_still_ends_the_turn(self):
        """Measured on a real call, and the reason it went nowhere.

        The greeting spoke and then nothing happened. `case _` swallowed
        TurnInterrupted, and `Conversation` holds the microphone shut until a
        TurnComplete arrives - 448 events, not one turn_complete, and a
        caller talking into a gate with nobody behind it.
        """
        session = build(FakeEar("venum"), FakeBrain("sari"), mouth := FakeMouth())
        await session.send_audio(speech(2.0))
        await session.end_of_speech()
        await collect(session, 2)

        mouth.interrupt()
        events = await collect(session, 1)
        assert isinstance(events[0], TurnComplete)

    async def test_the_keepalive_holds_off_while_the_mouth_is_speaking(self):
        """Audio into a reply in flight reads as the caller interrupting.

        Which is how the turn above came to be interrupted in the first
        place: the keepalive was the only thing on that socket.
        """
        mouth = FakeMouth()
        session = CascadeSession(
            FakeEar("venum"), FakeBrain("sari"), mouth, rate=RATE, keepalive_s=0.01
        )
        async with session:
            await session.send_audio(speech(2.0))
            await session.end_of_speech()
            await collect(session, 2)
            sent = mouth.kept_alive
            await asyncio.sleep(0.05)
            # Still speaking - the mouth has not reported its turn done.
            assert mouth.kept_alive == sent
            mouth.speak_back(b"\x01")
            await collect(session, 2)
            await asyncio.sleep(0.05)
            assert mouth.kept_alive > sent

    async def test_the_mouth_is_kept_open_while_it_has_nothing_to_say(self):
        """Found by running a whole conversation through this class.

        The Live API closed the socket with 1008 "client inactivity" between
        utterances. The microphone pump never lets that happen on the Live
        path - when the gate is shut it substitutes SILENCE rather than
        nothing - and the cascade's mouth is the one Live socket in the
        system that is never sent audio at all. A customer who paused to
        think would have been hung up on.
        """
        mouth = FakeMouth()
        session = CascadeSession(
            FakeEar(), FakeBrain(), mouth, rate=RATE, keepalive_s=0.01
        )
        async with session:
            await asyncio.sleep(0.05)
        assert mouth.kept_alive >= 2

    async def test_the_keepalive_stops_with_the_call(self):
        mouth = FakeMouth()
        session = CascadeSession(
            FakeEar(), FakeBrain(), mouth, rate=RATE, keepalive_s=0.01
        )
        async with session:
            await asyncio.sleep(0.03)
        sent = mouth.kept_alive
        await asyncio.sleep(0.05)
        assert mouth.kept_alive == sent

    async def test_opening_it_opens_the_mouth(self):
        mouth = FakeMouth()
        async with build(FakeEar(), FakeBrain(), mouth):
            assert mouth.opened
        assert not mouth.opened

    def test_the_mouth_is_told_it_is_a_loudspeaker_and_not_an_agent(self):
        """A session still holding the persona would answer the text, not read it."""
        lowered = SPEAK_ONLY.lower()
        assert "word for word" in lowered
        assert "do not answer" in lowered


class SlowEar(FakeEar):
    """An ear that takes a moment, so more speech can arrive while the first
    utterance is still being worked out."""

    def __init__(self, *answers: str | Exception) -> None:
        super().__init__(*answers)
        self.started: list[int] = []

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        self.started.append(len(pcm))
        await asyncio.sleep(0.05)
        return await super().hear(pcm, rate=rate)


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    until = loop.time() + timeout
    while not predicate():
        if loop.time() > until:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


class TestAQuestionWithAPauseInIt:
    """2026-11-26, the first Gemini-only call: "Mudhal phone stock-la
    irukka? ... EMI evvalavu?" ended at the pause, and the EMI half was
    never answered. The first fix cancelled the answer and restarted it, which
    made every repeat cost another round: 4-6 seconds to the first audio
    already. Now the answer in flight is finished, and the next question is
    answered after it."""

    async def test_the_next_question_is_answered_after_the_first_is_spoken(self):
        ear = SlowEar("stock irukka?", "EMI evvalavu?")
        brain = FakeBrain("Stock irukku.", "EMI 652 rupees.")
        mouth = FakeMouth()
        session = build(ear, brain, mouth)

        seen: list[Any] = []

        async def pump() -> None:
            async for event in session.events():
                seen.append(event)

        pumping = asyncio.create_task(pump())
        try:
            await session.send_audio(speech(1.0))
            await session.end_of_speech()
            await asyncio.sleep(0)  # the first turn is under way
            await session.send_audio(speech(0.8))
            await session.speech_continued()

            # Nothing was cancelled: the first question is the one asked.
            await asyncio.wait_for(session._thinking, 1.0)
            assert ear.started == [len(speech(1.0))]
            assert brain.asked == ["stock irukka?"]
            await _wait_for(lambda: mouth.said == ["Stock irukku."])

            # The first answer finishes; the second question is answered next.
            mouth.speak_back(bytes(64), turn_id=7)
            await _wait_for(lambda: len(mouth.said) == 2)
            assert brain.asked == ["stock irukka?", "EMI evvalavu?"]
            assert mouth.said == ["Stock irukku.", "EMI 652 rupees."]
        finally:
            pumping.cancel()

    async def test_speech_during_the_answer_waits_in_the_buffer(self):
        ear = FakeEar("stock irukka?")
        brain = FakeBrain("Stock irukku.")
        session = build(ear, brain, FakeMouth())

        await session.send_audio(speech(1.0))
        await session.end_of_speech()
        await asyncio.wait_for(session._thinking, 1.0)

        await session.send_audio(speech(0.8))
        await session.speech_continued()
        assert ear.heard == [len(speech(1.0))]
        assert session._queued is True
        assert len(session._buffer) == len(speech(0.8))

    async def test_a_blip_is_not_a_question(self):
        ear = SlowEar("stock irukka?")
        brain = FakeBrain("Stock irukku.")
        session = build(ear, brain, FakeMouth())

        await session.send_audio(speech(1.0))
        await session.end_of_speech()
        await asyncio.sleep(0)
        await session.send_audio(speech(0.1))
        await session.speech_continued()
        await asyncio.wait_for(session._thinking, 1.0)

        assert ear.started == [len(speech(1.0))]
        assert brain.asked == ["stock irukka?"]
        assert session._queued is False
