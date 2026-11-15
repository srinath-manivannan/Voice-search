"""The second ear, and the two numbers it reports.

The module exists to be read off a trace, so most of what is asserted here is
what lands in the trace rather than what is returned. The one behaviour worth
more than the measurement is that nothing in it can take a call down.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.voice.shadow import MAX_PENDING, ShadowEar, agreement, digits, words
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


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, **detail: Any) -> None:
        self.events.append((kind, detail))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def one(self, kind: str) -> dict[str, Any]:
        found = [detail for name, detail in self.events if name == kind]
        assert found, f"no {kind} in {self.kinds()}"
        return found[0]


class FakeWhisper:
    """Answers with the texts it was given, in order, or raises."""

    def __init__(self, *answers: str | Exception, seconds: float = 4.0) -> None:
        self._answers = list(answers)
        self._seconds = seconds
        self.calls = 0

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        self.calls += 1
        answer = self._answers.pop(0) if self._answers else "…"
        if isinstance(answer, Exception):
            raise answer
        return Heard(
            text=answer,
            seconds=self._seconds,
            billed_seconds=max(self._seconds, 10.0),
            model="fake",
        )


# --- the two measurements -------------------------------------------------


class TestAgreement:
    def test_the_same_line_twice(self):
        assert agreement("enakku phone venum", "enakku phone venum") == 1.0

    def test_punctuation_and_case_are_not_a_disagreement(self):
        assert agreement("Enakku phone venum!", "enakku phone venum") == 1.0

    def test_one_romanisation_apart_is_one_word_apart(self):
        """Tanglish spelling varies and that is not an error.

        "venum" against "vendum" is the same sentence. A character-level
        score would punish it twice - once for the spelling and again for the
        length - so this is deliberately word-level and deliberately lenient.
        """
        score = agreement("enakku oru phone venum", "enakku oru phone vendum")
        assert 0.7 < score < 1.0

    def test_two_different_sentences(self):
        assert agreement("phone venum", "laptop vேndaam") < 0.5

    def test_both_empty_is_agreement_not_a_crash(self):
        assert agreement("", "") == 1.0

    def test_words_strips_punctuation_without_eating_tamil(self):
        assert words("வேணும், 20,000!") == ["வேணும்", "20", "000"]


class TestDigits:
    def test_commas_come_out(self):
        assert digits("20,000 to 25,000 kulla") == {"20000", "25000"}

    def test_the_same_number_twice_is_one_number(self):
        assert digits("20000 venum, 20000 dhaan") == {"20000"}

    def test_a_line_with_no_numbers(self):
        assert digits("camera nalla irukkanum") == set()

    def test_tamil_words_for_a_number_do_not_count_as_the_number(self):
        """This is the finding, not a shortcoming of the test.

        prefetch.py reads a budget with a regular expression over digit runs.
        If the second ear writes "irupathaayiram" where the first wrote
        "20000", the prefetch will not fire on it and the search will be
        wrong - so the mismatch is reported rather than smoothed over.
        """
        assert digits("irupathaayiram kulla") == set()
        assert digits("20000 kulla") == {"20000"}


# --- running it -----------------------------------------------------------


class TestTheEar:
    async def test_it_compares_an_utterance_against_the_live_transcript(self):
        trace = FakeTrace()
        ear = ShadowEar(FakeWhisper("enakku 20000 kulla phone venum"), trace, rate=RATE)

        ear.hears(speech(2.0))
        ear.hears(speech(2.0))
        ear.caller_stopped()
        ear.live_said("enakku 20000 kulla phone vendum")
        await asyncio.gather(*ear.in_flight)
        await ear.settle()

        compared = trace.one("shadow_compared")
        assert compared["live"] == "enakku 20000 kulla phone vendum"
        assert compared["shadow"] == "enakku 20000 kulla phone venum"
        assert compared["digits_match"] is True
        assert 0.7 < compared["agreement"] < 1.0
        assert compared["seconds"] == pytest.approx(4.0)

    async def test_a_high_score_with_the_budget_misheard_is_still_a_mismatch(self):
        """The one failure that matters, and the one a score would hide.

        "25000" against "20000" is one word in twelve - an agreement of about
        0.9 - and it is a different phone, a different search and a wrong
        answer. The digits are compared separately for exactly this.
        """
        trace = FakeTrace()
        ear = ShadowEar(
            FakeWhisper("enakku 25000 kulla oru phone venum"), trace, rate=RATE
        )

        ear.hears(speech(3.0))
        ear.caller_stopped()
        ear.live_said("enakku 20000 kulla oru phone venum")
        await asyncio.gather(*ear.in_flight)
        await ear.settle()

        compared = trace.one("shadow_compared")
        assert compared["agreement"] > 0.8
        assert compared["digits_match"] is False

    async def test_nothing_is_sent_when_the_caller_said_nothing(self):
        trace = FakeTrace()
        whisper = FakeWhisper("…")
        ear = ShadowEar(whisper, trace, rate=RATE)

        ear.caller_stopped()
        await asyncio.sleep(0)

        assert whisper.calls == 0
        assert ear.tally.utterances == 0

    async def test_the_buffer_restarts_after_every_turn(self):
        trace = FakeTrace()
        whisper = FakeWhisper("one", "two")
        ear = ShadowEar(whisper, trace, rate=RATE)

        ear.hears(speech(1.0))
        ear.caller_stopped()
        ear.live_said("one")
        ear.hears(speech(1.0))
        ear.caller_stopped()
        ear.live_said("two")
        await asyncio.gather(*ear.in_flight)
        await ear.settle()

        assert whisper.calls == 2
        assert ear.tally.compared == 2

    async def test_a_transcription_that_blows_up_leaves_the_call_alone(self):
        trace = FakeTrace()
        ear = ShadowEar(
            FakeWhisper(WhisperError("groq returned 503")), trace, rate=RATE
        )

        ear.hears(speech(2.0))
        ear.caller_stopped()
        ear.live_said("enakku phone venum")
        await asyncio.gather(*ear.in_flight)
        await ear.settle()

        assert ear.tally.failed == 1
        assert ear.tally.compared == 0
        assert "shadow_failed" in trace.kinds()

    async def test_a_transcript_the_live_api_never_reported_is_recorded(self):
        """The direction that would flatter the cascade, so it is not skipped."""
        trace = FakeTrace()
        ear = ShadowEar(FakeWhisper("stock irukka"), trace, rate=RATE)

        ear.hears(speech(2.0))
        ear.caller_stopped()
        await asyncio.gather(*ear.in_flight)
        await ear.settle()

        assert trace.one("shadow_unpaired")["shadow"] == "stock irukka"
        assert ear.tally.compared == 0

    async def test_settle_does_not_wait_for_a_transcription_still_in_flight(self):
        """A slow Groq must not sit between a caller and their next answer."""
        trace = FakeTrace()
        started = asyncio.Event()

        class Slow:
            async def hear(self, pcm: bytes, *, rate: int) -> Heard:
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("should have been cancelled")

        ear = ShadowEar(Slow(), trace, rate=RATE)
        ear.hears(speech(2.0))
        ear.caller_stopped()
        ear.live_said("enakku phone venum")
        await started.wait()

        await asyncio.wait_for(ear.settle(), 1.0)
        assert trace.kinds() == []

    async def test_more_pending_than_the_cap_drops_the_oldest(self):
        trace = FakeTrace()
        ear = ShadowEar(FakeWhisper(), trace, rate=RATE)

        for _ in range(MAX_PENDING + 2):
            ear.hears(speech(1.0))
            ear.caller_stopped()

        assert ear.tally.dropped == 2
        assert trace.kinds().count("shadow_dropped") == 2


class TestTheTally:
    async def test_close_writes_one_line_for_the_whole_call(self):
        trace = FakeTrace()
        ear = ShadowEar(
            FakeWhisper("20000 kulla phone", "25000 dhaan"), trace, rate=RATE
        )

        ear.hears(speech(2.0))
        ear.caller_stopped()
        ear.live_said("20000 kulla phone")
        ear.hears(speech(2.0))
        ear.caller_stopped()
        ear.live_said("20000 dhaan")
        await ear.close()

        tally = trace.one("shadow_ear")
        assert tally["utterances"] == 2
        assert tally["compared"] == 2
        # The first utterance agreed about the budget and the second did not.
        assert tally["digits_matched"] == 1
        assert tally["failed"] == 0
        # Two requests under the ten-second floor.
        assert tally["billed_seconds"] == pytest.approx(20.0)
        assert tally["usd"] == pytest.approx(20 / 3600 * 0.04, abs=1e-5)

    async def test_close_is_safe_on_a_call_where_nobody_spoke(self):
        trace = FakeTrace()
        ear = ShadowEar(FakeWhisper(), trace, rate=RATE)
        await ear.close()
        assert trace.one("shadow_ear")["utterances"] == 0
