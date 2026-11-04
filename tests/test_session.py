"""Barge-in is the behaviour worth testing here; the rest is plumbing."""

from __future__ import annotations

import asyncio
import struct

from src.voice.events import AudioOut, TurnComplete, TurnInterrupted
from src.voice.session import Conversation
from src.voice.usage import TurnUsage
from src.voice.vad import EnergyVad
from tests.fakes import FakeSink, FakeSource

FRAME_SAMPLES = 320
SILENCE = b"\x00" * FRAME_SAMPLES * 2
SPEECH = struct.pack(f"<{FRAME_SAMPLES}h", *([4000, -4000] * (FRAME_SAMPLES // 2)))


class StubModel:
    """Stands in for GeminiLiveSession: replays events, records sent audio.

    `keep_open` models a socket that stays up after the scripted events, which
    is what a real session does. Without it the model pump finishes first and
    `run()` correctly cancels the microphone - correct in production, useless
    in a test that needs the microphone to keep feeding.
    """

    def __init__(self, events: list, keep_open: bool = False) -> None:
        self._events = list(events)
        self._keep_open = keep_open
        self.sent: list[bytes] = []
        self.ends = 0
        self.turn_id = 1

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def end_of_speech(self) -> None:
        # The real adapter advances its turn id here, which is the whole point.
        self.turn_id += 1
        self.ends += 1

    async def events(self):
        for event in self._events:
            yield event
            await asyncio.sleep(0)
        if self._keep_open:
            await asyncio.Event().wait()


async def test_every_microphone_frame_reaches_the_model():
    """Silence included - the server does not bill it and gating risks turn detection."""
    source = FakeSource([SILENCE, SILENCE, SILENCE])
    model = StubModel([])
    await Conversation(model, source, FakeSink()).run()
    assert len(model.sent) == 3


async def test_local_speech_while_the_agent_talks_flushes_playback():
    """The whole point: stop without waiting for the server to agree."""
    # Sustained speech: a real interrupter talks over the agent for a moment,
    # which is what separates them from echo of the agent's own voice.
    source = FakeSource([SILENCE] * 40 + [SPEECH] * 20)
    sink = FakeSink()
    model = StubModel([AudioOut(turn_id=1, pcm=b"agent talking")], keep_open=True)

    conversation = Conversation(
        model, source, sink, vad=EnergyVad(speech_frames_to_trigger=3)
    )
    # Stand the agent outside its own grace window: this is a caller speaking
    # over a reply already in progress, not the agent hearing itself start.
    conversation.BARGE_IN_GRACE_SECONDS = 0.0
    await conversation.run()

    assert sink.flushes >= 1, "playback was not stopped when the caller spoke"
    assert b"agent talking" in b"".join(sink.discarded)


async def test_audio_from_an_abandoned_turn_never_reaches_the_speaker():
    """Late chunks for a turn the caller talked over must be dropped."""
    sink = FakeSink()
    model = StubModel(
        [
            AudioOut(turn_id=1, pcm=b"first"),
            TurnInterrupted(turn_id=1),
            AudioOut(turn_id=1, pcm=b"stale, arrived after the interruption"),
            AudioOut(turn_id=2, pcm=b"the new answer"),
        ]
    )
    await Conversation(model, FakeSource([], keep_open=True), sink).run()

    heard = b"".join(sink.written)
    assert b"stale" not in heard
    assert b"the new answer" in heard


async def test_turn_completion_records_latency_and_the_hidden_thinking_tokens():
    usage = TurnUsage(response_audio=205, thoughts=126)
    model = StubModel([TurnComplete(turn_id=1, usage=usage, first_audio_ms=742.0)])

    result = await Conversation(model, FakeSource([], keep_open=True), FakeSink()).run()

    assert result.latencies() == [742.0]
    record = result.turns[0]
    assert record.response_tokens == 205
    # Thinking is tracked separately because it is billed and does not appear
    # in totalTokenCount.
    assert record.thinking_tokens == 126


async def test_the_agent_does_not_interrupt_itself_the_moment_it_starts():
    """Echo of the agent's own voice must not read as a barge-in.

    The microphone hears the speaker. Browser echo cancellation removes most of
    it and not all, and the remainder is speech-shaped - precisely what the VAD
    exists to detect. Without a grace period the agent flushes its own reply a
    few frames after starting it, and the caller hears a syllable and then
    silence, over and over.
    """
    sink = FakeSink()
    source = FakeSource([SILENCE] * 40 + [SPEECH] * 6, keep_open=False)
    model = StubModel([AudioOut(turn_id=1, pcm=b"the agent answering")], keep_open=True)

    conversation = Conversation(
        model, source, sink, vad=EnergyVad(speech_frames_to_trigger=3)
    )
    await conversation.run()

    # 46 frames at 20 ms is under a second of wall clock, all of it inside the
    # grace window, so nothing should have been flushed.
    assert sink.flushes == 0, "the agent interrupted itself"
    assert b"the agent answering" in b"".join(sink.written)


async def test_a_real_interruption_still_lands_once_the_grace_window_passes():
    sink = FakeSink()
    model = StubModel([AudioOut(turn_id=1, pcm=b"talking")], keep_open=True)
    conversation = speaking_conversation(model, [SILENCE] * 10 + [SPEECH] * 25, sink)
    await conversation._pump_microphone()

    assert sink.flushes >= 1, "a genuine barge-in was ignored"


async def test_the_end_of_the_caller_s_turn_is_signalled():
    """Without this the turn id never advances, and that is fatal.

    Every reply would be tagged turn 0 for the whole call. The first
    interruption puts turn 0 in the abandoned set, and from then on every chunk
    the model sends is dropped before it reaches the speaker - the agent falls
    silent for good while its transcript keeps scrolling. That is exactly the
    "sometimes it replies, sometimes it does not" this fixes.
    """
    model = StubModel([], keep_open=True)
    # Speech, then enough silence for the VAD to release the turn.
    source = FakeSource([SILENCE] * 5 + [SPEECH] * 10 + [SILENCE] * 40)
    await Conversation(model, source, FakeSink(), vad=EnergyVad()).run()

    assert model.ends == 1, "the caller stopping was never signalled"
    assert model.turn_id == 2, "the turn id did not advance"


async def test_silence_alone_never_signals_a_turn():
    """A quiet line is not a caller who has finished speaking."""
    model = StubModel([], keep_open=True)
    await Conversation(
        model, FakeSource([SILENCE] * 60), FakeSink(), vad=EnergyVad()
    ).run()
    assert model.ends == 0


async def test_each_turn_gets_its_own_id_so_abandoning_one_spares_the_next():
    """Two turns, but only once the model has answered the first.

    Two bursts of speech used to produce two signals regardless. They now
    produce one, because signalling again while a reply is still being
    generated is what made the model abandon it - eleven times in a single
    forty-five second call, which left it producing no audio at all.
    """
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model, FakeSource([SPEECH] * 10 + [SILENCE] * 40), FakeSink(), vad=EnergyVad()
    )
    await conversation._pump_microphone()
    assert model.ends == 1 and model.turn_id == 2

    # The model replies and finishes, which releases the latch.
    conversation._awaiting_reply = False
    conversation._source = FakeSource([SPEECH] * 10 + [SILENCE] * 40)
    await conversation._pump_microphone()
    assert model.ends == 2 and model.turn_id == 3


def speaking_conversation(model, frames, sink=None):
    """A conversation with the agent already holding the floor.

    Set directly rather than driven through the model pump: otherwise whether
    the gate is closed when the microphone frames arrive depends on task
    scheduling, and the test passes or fails by luck.
    """
    conversation = Conversation(
        model, FakeSource(frames), sink or FakeSink(), vad=EnergyVad()
    )
    conversation._agent_speaking = True
    conversation._agent_started_at = 0.0  # long past the grace window
    return conversation


async def test_the_microphone_is_held_back_while_the_agent_speaks():
    """The model must not hear the agent's own voice.

    Browser echo cancellation leaves enough behind that the model recognises
    it as speech, concludes the caller interrupted, and cuts its own sentence
    off - which is what made the greeting stop partway through. AI-voice gates
    the same way: while the agent holds the floor, nothing is forwarded unless
    a genuine interruption is confirmed.
    """
    model = StubModel([], keep_open=True)
    # Quiet-room echo: never loud enough, never sustained enough to count.
    conversation = speaking_conversation(model, [SILENCE] * 30)
    await conversation._pump_microphone()

    # Silence goes out, the caller's audio does not. Sending nothing at all
    # would leave the connection idle for the length of every reply.
    assert all(f == bytes(len(f)) for f in model.sent), (
        "the agent's own audio window was forwarded to the model"
    )


async def test_a_confirmed_interruption_forwards_the_run_up():
    """The caller's first syllable must not be lost to the gate."""
    model = StubModel([], keep_open=True)
    sink = FakeSink()
    conversation = speaking_conversation(model, [SPEECH] * 25, sink)
    await conversation._pump_microphone()

    assert sink.flushes >= 1, "a sustained interruption did not stop playback"
    # Held frames are released on confirmation, so the model hears the word
    # that started the interruption rather than only its tail.
    assert len(model.sent) > 0


async def test_the_floor_is_held_from_the_moment_the_agent_is_asked_to_speak():
    """Not from when its first audio chunk arrives, some 700 ms later.

    That gap cost us the greeting three times over. The microphone was still
    open across it, a rise and fall in room noise read as the caller finishing
    a sentence, end_of_speech told the model the caller had stopped, and the
    model abandoned its greeting to answer. Claiming the floor on construction
    is what closes it.
    """
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model,
        FakeSource([SILENCE] * 10 + [SPEECH] * 4 + [SILENCE] * 40),
        FakeSink(),
        vad=EnergyVad(),
        agent_opens=True,
    )
    await conversation._pump_microphone()

    # Only silence forwarded and, crucially, no end-of-turn signalled - the
    # model is left to finish its greeting.
    assert all(f == bytes(len(f)) for f in model.sent)
    assert model.ends == 0, "the greeting was cut short by a false end of turn"


async def test_without_agent_opens_the_caller_speaks_first_as_before():
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model,
        FakeSource([SILENCE] * 5 + [SPEECH] * 10 + [SILENCE] * 40),
        FakeSink(),
        vad=EnergyVad(),
    )
    await conversation._pump_microphone()

    assert model.sent, "the caller's audio never reached the model"
    assert model.ends == 1


async def test_quiet_echo_does_not_clear_the_barge_in_threshold():
    """Taken straight from a trace: rms 103 against a noise floor of 1.

    In a quiet room the floor decays to its minimum, and at that point a purely
    relative threshold - six times the floor - means "louder than six", which
    every echo clears. The absolute floor is what makes the test mean anything.
    """
    import struct

    quiet_echo = struct.pack(
        f"<{FRAME_SAMPLES}h", *([103, -103] * (FRAME_SAMPLES // 2))
    )
    model = StubModel([], keep_open=True)
    sink = FakeSink()
    # Loud enough first to get the VAD into speech, then the quiet echo that
    # used to be mistaken for the caller.
    conversation = speaking_conversation(model, [SPEECH] * 4 + [quiet_echo] * 30, sink)
    await conversation._pump_microphone()

    assert sink.flushes == 0, "quiet echo was treated as the caller interrupting"


async def test_only_one_turn_is_signalled_until_the_model_answers():
    """Eleven signals in forty-five seconds, and not one completed turn.

    Every end_of_speech makes the model start a reply and abandon the one in
    progress. A caller who pauses between phrases was being served nothing at
    all - after sixteen seconds the model had stopped producing audio.
    """
    model = StubModel([], keep_open=True)
    # Three bursts of speech with pauses between them, as a person talks.
    frames = ([SPEECH] * 8 + [SILENCE] * 30) * 3
    conversation = Conversation(model, FakeSource(frames), FakeSink(), vad=EnergyVad())
    await conversation._pump_microphone()

    assert model.ends == 1, (
        f"signalled {model.ends} turns while the model had answered none"
    )


async def test_the_latch_releases_when_the_model_finishes():
    model = StubModel([], keep_open=True)
    conversation = Conversation(
        model, FakeSource([SPEECH] * 8 + [SILENCE] * 30), FakeSink(), vad=EnergyVad()
    )
    await conversation._pump_microphone()
    assert model.ends == 1 and conversation._awaiting_reply is True

    # The model answers and completes; the next pause may signal again.
    conversation._awaiting_reply = False
    conversation._heard_speech_this_turn = False
    conversation._source = FakeSource([SPEECH] * 8 + [SILENCE] * 30)
    await conversation._pump_microphone()
    assert model.ends == 2


async def test_the_latch_is_released_when_the_model_finishes_its_turn():
    """Without this the first turn is the only turn.

    A trace of a forty-second call showed one end_of_speech and four separate
    stretches of agent audio: the caller spoke repeatedly and the model was
    told once. _awaiting_reply was set and never cleared, because the line
    clearing it had silently failed to apply.
    """
    usage = TurnUsage(response_audio=100)
    model = StubModel(
        [TurnComplete(turn_id=1, usage=usage, first_audio_ms=700.0)], keep_open=False
    )
    conversation = Conversation(
        model, FakeSource([], keep_open=True), FakeSink(), vad=EnergyVad()
    )
    conversation._awaiting_reply = True

    await conversation._pump_model()

    assert conversation._awaiting_reply is False, "the latch was never released"
    assert conversation.trace.summary()["turns"] == 1


async def test_a_completed_turn_is_recorded_in_the_trace():
    """turns read 0 on a call where turns plainly completed."""
    usage = TurnUsage(response_audio=120, thoughts=0)
    model = StubModel(
        [TurnComplete(turn_id=2, usage=usage, first_audio_ms=880.0)], keep_open=False
    )
    conversation = Conversation(
        model, FakeSource([], keep_open=True), FakeSink(), vad=EnergyVad()
    )
    await conversation._pump_model()

    summary = conversation.trace.summary()
    assert summary["turns"] == 1
    assert summary["p50_first_audio_ms"] == 880.0


async def test_the_opening_greeting_cannot_be_cut_off():
    """It is one sentence, and losing it reads as a broken line.

    A trace caught it ending at 3.2 seconds on a frame at rms 949 - loud
    enough to clear the threshold and impossible to separate from a louder
    speaker. Everything after the greeting can still be interrupted.
    """
    model = StubModel([], keep_open=True)
    sink = FakeSink()
    conversation = speaking_conversation(model, [SPEECH] * 40, sink)
    conversation._in_greeting = True
    await conversation._pump_microphone()

    assert sink.flushes == 0, "the greeting was interrupted"


async def test_interruption_works_again_once_the_greeting_is_done():
    model = StubModel([], keep_open=True)
    sink = FakeSink()
    conversation = speaking_conversation(model, [SPEECH] * 40, sink)
    conversation._in_greeting = False
    await conversation._pump_microphone()

    assert sink.flushes >= 1, "a genuine interruption was ignored after the greeting"


async def test_silence_is_sent_while_the_gate_is_shut_not_nothing():
    """A stream that goes quiet can be timed out and closed.

    A trace showed 440 frames held and not a single byte sent to the model
    across eleven seconds. Withholding the caller's audio is right - it
    carries the agent's own echo - but sending nothing at all leaves the
    connection idle for the length of every reply. Silence keeps it alive,
    and Phase 0 measured that silence is not billed.
    """
    model = StubModel([], keep_open=True)
    conversation = speaking_conversation(model, [SPEECH] * 20)
    conversation._in_greeting = True
    await conversation._pump_microphone()

    assert len(model.sent) == 20, "the model was sent nothing while the gate was shut"
    assert all(frame == bytes(len(frame)) for frame in model.sent), (
        "the caller's own audio was forwarded instead of silence"
    )


async def test_the_tail_of_the_agent_s_own_sentence_does_not_end_a_turn():
    """Measured: end_of_speech fired 4 ms after the agent stopped.

    TurnComplete means the model stopped generating, not that the speaker
    stopped playing - the browser still has audio queued when the gate opens,
    and the microphone hears the last of the agent's own sentence. One such
    frame counted as "the caller spoke", and the next quiet frame signalled
    the end of a turn nobody had taken.

    It also started the latency clock, so the next genuine reply was timed
    from before the caller had opened their mouth. That is where a p95 of
    eight seconds came from, and most of it was never dead air at all.
    """
    model = StubModel([], keep_open=True)
    # Four frames is 80 ms - an echo tail, not a syllable.
    source = FakeSource([SILENCE] * 5 + [SPEECH] * 4 + [SILENCE] * 40)
    await Conversation(model, source, FakeSink(), vad=EnergyVad()).run()

    assert model.ends == 0, "an 80 ms burst was taken for the caller's turn"


async def test_a_real_syllable_still_ends_the_turn():
    """The guard must not swallow short answers. "Aamaa" is a whole turn."""
    model = StubModel([], keep_open=True)
    source = FakeSource([SILENCE] * 5 + [SPEECH] * 12 + [SILENCE] * 40)
    await Conversation(model, source, FakeSink(), vad=EnergyVad()).run()

    assert model.ends == 1
