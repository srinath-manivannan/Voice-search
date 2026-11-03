"""The call trace.

This exists because four separate bugs in this service each took three or four
attempts to find, and every wrong attempt was a round trip through a person.
What was missing each time was the same thing: any record of what the call
decided. These tests pin down that the record answers the questions that were
actually asked.
"""

from __future__ import annotations

from src.voice.session import Conversation
from src.voice.trace import CallTrace
from src.voice.vad import EnergyVad
from tests.fakes import FakeSink, FakeSource
from tests.test_session import SILENCE, SPEECH, StubModel


def test_events_are_ordered_and_timed_from_the_start_of_the_call():
    trace = CallTrace()
    trace.record("call_open")
    trace.record("agent_floor", turn=1)

    assert [e.kind for e in trace.events] == ["call_open", "agent_floor"]
    assert all(e.at_ms >= 0 for e in trace.events)
    assert trace.events[1].at_ms >= trace.events[0].at_ms


def test_the_record_is_bounded_so_a_long_call_cannot_grow_it_forever():
    trace = CallTrace(limit=5)
    for _ in range(50):
        trace.record("audio_out")
    assert len(trace.events) == 5


def test_the_summary_separates_our_interruptions_from_the_model_s():
    """Telling these apart was the difference between two different bugs."""
    trace = CallTrace()
    trace.record("barge_in_local")
    trace.record("barge_in_local")
    trace.record("interrupted_by_model")

    summary = trace.summary()
    assert summary["barge_ins_local"] == 2
    assert summary["interrupts_from_model"] == 1


def test_the_summary_shows_audio_that_was_produced_but_never_heard():
    """The exact shape of the turn-id bug: chunks arriving and being dropped."""
    trace = CallTrace()
    for _ in range(9):
        trace.record("audio_out", turn=1)
    for _ in range(4):
        trace.record("audio_dropped", turn=0)

    summary = trace.summary()
    assert summary["audio_chunks"] == 9
    assert summary["audio_dropped_abandoned"] == 4


def test_the_summary_reports_percentiles_without_a_separate_measurement():
    trace = CallTrace()
    for ms in (400, 700, 1500):
        trace.record("turn_complete", first_audio_ms=ms)

    summary = trace.summary()
    assert summary["turns"] == 3
    assert summary["p50_first_audio_ms"] == 700
    assert summary["p95_first_audio_ms"] == 1500


def test_an_empty_call_summarises_without_blowing_up():
    """A call where nothing happened is exactly when this gets read."""
    summary = CallTrace().summary()
    assert summary["turns"] == 0
    assert summary["p50_first_audio_ms"] is None


async def test_a_conversation_records_the_gate_holding_frames_back():
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model, FakeSource([SILENCE] * 20), FakeSink(), vad=EnergyVad(), agent_opens=True
    )
    await conversation._pump_microphone()

    held = conversation.trace.of_kind("gate_held")
    assert len(held) == 20, "the gate's decisions were not recorded"
    assert conversation.trace.summary()["frames_held_by_gate"] == 20


async def test_a_conversation_records_signalling_the_end_of_a_turn():
    """Its absence was the bug behind replies stopping for good."""
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model, FakeSource([SPEECH] * 10 + [SILENCE] * 40), FakeSink(), vad=EnergyVad()
    )
    await conversation._pump_microphone()

    assert conversation.trace.summary()["turns_signalled"] == 1


def test_saving_writes_a_file_that_can_be_handed_over(tmp_path):
    import json

    trace = CallTrace()
    trace.record("call_open")
    trace.record("turn_complete", first_audio_ms=900)

    path = trace.save(tmp_path)
    written = json.loads(path.read_text(encoding="utf-8"))

    assert written["summary"]["turns"] == 1
    assert [e["kind"] for e in written["events"]] == ["call_open", "turn_complete"]
