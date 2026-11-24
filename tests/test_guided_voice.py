"""The recorded lines: that they exist, and that they say what the screen says.

Nobody reviews a waveform. So the things a reviewer cannot see are asserted
here instead - every sentence has a recording, every recording was made from
the sentence as it is written today, and every one of them was played back
and heard as that sentence.

The first test is meant to fail. Edit a question in `script.py` and it goes
red until `make guided-voice` has been run, which is the whole of the
process for changing what the guided call says.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.guided import script
from src.guided.script import SCRIPTS, SPOKEN
from src.guided.voice import (
    RECORDINGS,
    Recordings,
    read_manifest,
    wav,
    wav_seconds,
    write_manifest,
)
from src.tools import record_guided
from src.tools.record_guided import ALIKE_ENOUGH, likeness, refusal


def test_every_line_has_a_recording_made_from_the_sentence_as_it_is_written_now():
    stale = sorted(set(SPOKEN) - Recordings().current)

    assert not stale, f"edited or never recorded - run `make guided-voice`: {stale}"


def test_every_sentence_the_call_can_say_is_one_that_gets_recorded():
    """A question added to a shelf and not to LINES would be asked in silence."""
    said = {key for shelf in SCRIPTS for key in _keys_of(shelf)}

    assert said <= set(script.LINES), said - set(script.LINES)
    assert len(set(SPOKEN.values())) == len(SPOKEN), "two names for one sentence"


def _keys_of(shelf) -> set[str]:
    keys = {facet.ask for facet in shelf.facets}
    for use in shelf.uses:
        if use.ask_next:
            keys.add(use.ask_next)
        keys |= {child.ask_next for child in use.then if child.ask_next}
    return keys


def test_every_recording_kept_was_played_back_and_heard_as_its_own_line():
    """The bar the recorder applies, applied again to what was committed.

    So a recording cannot arrive by hand, or from a run with the check
    switched off, without this saying so.
    """
    manifest = read_manifest()

    for key, text in SPOKEN.items():
        entry = manifest[key]
        assert entry["heard"], f"{key} was kept without being listened to"
        assert likeness(text, entry["heard"]) >= ALIKE_ENOUGH, (key, entry["heard"])


def test_every_recording_is_as_long_as_its_manifest_says_and_no_longer_than_a_line():
    manifest = read_manifest()

    for key in SPOKEN:
        seconds = wav_seconds((RECORDINGS / f"{key}.wav").read_bytes())
        assert seconds is not None, f"{key}.wav is not a recording"
        assert seconds == pytest.approx(manifest[key]["seconds"], abs=0.01)
        # Up to six seconds: the longest honest line ("sorry, nothing is in
        # stock for that...") runs 5.4 in English. Past this the model has
        # stopped reading and started talking.
        assert 0.6 <= seconds <= 6.0, f"{key} runs {seconds:.1f}s"


# --- which recordings may be played -------------------------------------------

LINES = {"ask_budget": "உங்க பட்ஜெட் எவ்வளவு?", "say_card": "இதோ முழு விவரம்."}


def recorded(
    folder: Path, made_from: dict[str, str], files: dict[str, bytes]
) -> Recordings:
    write_manifest({key: {"text": text} for key, text in made_from.items()}, folder)
    for key, data in files.items():
        (folder / f"{key}.wav").write_bytes(data)
    return Recordings(folder, LINES)


def clip() -> bytes:
    return wav(b"\x01\x00" * 24_000, 24_000)


def test_a_current_recording_is_named_and_an_unrecorded_line_is_not(tmp_path):
    held = recorded(
        tmp_path, {"ask_budget": LINES["ask_budget"]}, {"ask_budget": clip()}
    )

    assert held.for_line(LINES["ask_budget"]) == "ask_budget"
    assert held.for_line(LINES["say_card"]) is None
    assert held.for_line("a sentence nobody wrote") is None


def test_a_recording_of_the_sentence_as_it_used_to_be_is_not_played(tmp_path):
    """The screen says the new sentence. The voice must not say the old one."""
    held = recorded(tmp_path, {"ask_budget": "உங்க பட்ஜெட் என்ன?"}, {"ask_budget": clip()})

    assert held.for_line(LINES["ask_budget"]) is None and held.current == frozenset()


@pytest.mark.parametrize(
    "data",
    [
        None,  # in the manifest, not on disk
        b"",
        b"<html>404 not found</html>",
        clip()[:40],  # cut off inside the header
        wav(b"", 24_000),  # a header and no sound
        clip().replace(b"WAVEfmt ", b"WAVEjunk"),
    ],
)
def test_a_file_that_is_not_a_recording_is_not_offered_as_one(tmp_path, data):
    files = {} if data is None else {"ask_budget": data}

    held = recorded(tmp_path, {"ask_budget": LINES["ask_budget"]}, files)

    assert held.for_line(LINES["ask_budget"]) is None


def test_with_nothing_recorded_at_all_every_line_is_simply_shown(tmp_path):
    held = Recordings(tmp_path / "never-made", LINES)

    assert held.current == frozenset()


def test_a_wav_is_as_long_as_the_sound_put_into_it():
    assert wav_seconds(wav(b"\x00\x00" * 36_000, 24_000)) == pytest.approx(1.5)
    assert wav_seconds(wav(b"\x00\x00" * 8_000, 8_000)) == pytest.approx(1.0)


# --- what the recorder keeps ---------------------------------------------------
#
# Every "heard" below is what the first real run of the recorder was told,
# on 2026-11-23. Each was a clip that sounded fine and was wrong.


@pytest.mark.parametrize(
    ("meant", "heard"),
    [
        # "Greet the customer now" put a greeting on a question.
        ("உங்களுக்கு எது முக்கியம்?", "வணக்கம் உங்களுக்கு எது முக்கியம்?"),
        ("எவ்வளவு கெபாசிட்டி வேணும்?", "வணக்கம். எவ்வளவு கெப்பாசிட்டி வேணும்?"),
        # The question was answered instead of read.
        ("எந்த சைஸ் வேணும்?", "Yeah"),
        # The agent declined to read it and introduced itself.
        ("இதோ முழு விவரம்.", "மன்னிக்கவும். நான் பூர்விகா AI அசிஸ்டன்ட்."),
        # A different word, one letter away.
        ("எந்த சைஸ் வேணும்?", "எந்த சைடு வேணும்"),
        # Heard, but not in the script it has to be checked in.
        ("இதோ முழு விவரம்.", "Idho Muzhu Vivaram"),
    ],
)
def test_a_take_that_is_not_the_line_is_refused(meant: str, heard: str):
    assert refusal(meant, heard, 1.8) is not None


@pytest.mark.parametrize(
    ("meant", "heard"),
    [
        ("வணக்கம்! எந்த பொருள் பாக்கணும்?", "வணக்கம். எந்த பொருள் பார்க்கணும்?"),
        ("ஒரு நிமிஷம், பாக்குறேன்.", "ஒரு நிமிஷம் பாக்குறேன்."),
        (
            "இதோ உங்களுக்கு ஏத்த தேர்வுகள். ஒன்றை தொடுங்க.",
            "இதோ உங்களுக்கு ஏத்த தேர்வுகள், ஒன்றை தொடுங்கள்.",
        ),
    ],
)
def test_the_same_sentence_spelt_another_way_is_kept(meant: str, heard: str):
    """Spoken Tamil is written several ways. That is not a different line."""
    assert refusal(meant, heard, 1.8) is None


def test_a_clip_too_short_or_too_long_is_refused_whatever_it_was_heard_as():
    line = "உங்க பட்ஜெட் எவ்வளவு?"

    assert "too short" in (refusal(line, line, 0.3) or "")
    assert "too long" in (refusal(line, line, 14.0) or "")
    assert refusal(line, None, 1.5) is None, "unchecked is a choice the caller made"


def test_lines_are_spoken_by_the_speech_model_in_the_chosen_voice_with_no_instruction():
    """The Live model greeted, answered and refused English; a style
    instruction to the speech model was read out loud. The text alone."""
    source = inspect.getsource(record_guided.speak)

    assert "Speaker(" in source and "guided_voice" in source
    assert "greet(" not in source and "READER" not in source


def test_every_recording_is_in_the_one_voice():
    """Two speakers in one call is what a mixed folder sounds like."""
    manifest = read_manifest()

    voices = {manifest[key].get("voice") for key in SPOKEN}
    assert voices == {"ta-in-techagent-5"}, voices
