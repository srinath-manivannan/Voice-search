"""The VAD only has two jobs - start playback-stopping fast, and not fire on a quiet room."""

from __future__ import annotations

import struct

from src.voice.vad import EnergyVad, frame_rms

FRAME_SAMPLES = 320  # 20 ms at 16 kHz


def tone(amplitude: int, samples: int = FRAME_SAMPLES) -> bytes:
    """A square wave. Crude, but it has a known RMS equal to its amplitude."""
    return struct.pack(f"<{samples}h", *([amplitude, -amplitude] * (samples // 2)))


SILENCE = b"\x00" * FRAME_SAMPLES * 2
QUIET_ROOM = tone(60)
SPEECH = tone(4000)


def test_rms_of_silence_is_zero_and_of_a_square_wave_is_its_amplitude():
    assert frame_rms(SILENCE) == 0.0
    assert round(frame_rms(tone(1000))) == 1000


def test_an_odd_trailing_byte_does_not_crash_a_live_call():
    assert frame_rms(b"\x01\x02\x03") >= 0.0
    assert frame_rms(b"") == 0.0


def test_a_quiet_room_never_counts_as_speech():
    vad = EnergyVad()
    for _ in range(200):
        assert not vad.accept(QUIET_ROOM).is_speech


def test_speech_triggers_within_the_debounce_window():
    vad = EnergyVad(speech_frames_to_trigger=3)
    for _ in range(50):
        vad.accept(QUIET_ROOM)

    assert not vad.accept(SPEECH).is_speech  # 1
    assert not vad.accept(SPEECH).is_speech  # 2
    # Three frames is 60 ms - fast enough to flush playback well inside the
    # 300 ms barge-in target.
    assert vad.accept(SPEECH).is_speech  # 3


def test_speech_is_held_through_a_short_pause():
    """A gap between words must not end the turn."""
    vad = EnergyVad(speech_frames_to_trigger=3, silence_frames_to_release=25)
    for _ in range(10):
        vad.accept(SPEECH)
    assert vad.in_speech

    for _ in range(20):  # 400 ms of thinking pause
        vad.accept(QUIET_ROOM)
    assert vad.in_speech, "a 400 ms pause ended the turn"

    for _ in range(10):
        vad.accept(QUIET_ROOM)
    assert not vad.in_speech


def test_the_noise_floor_follows_a_room_that_gets_quieter():
    vad = EnergyVad()
    for _ in range(50):
        vad.accept(tone(150))
    noisy = vad.noise_floor

    for _ in range(50):
        vad.accept(tone(10))
    assert vad.noise_floor < noisy
