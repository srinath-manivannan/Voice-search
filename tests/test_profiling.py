"""The CPU target, and the harness that checks it.

Rule 70 asks for a CPU target verified by profiling. The target is stated in
`src/tools/profile_call.py`: one connected call under 6% of a core, so
sixteen concurrent calls leave a single-core container at the 60% the rule
names. These tests keep the harness honest rather than re-running it - a
profile in a unit test would measure the test runner.
"""

from __future__ import annotations

from src.tools import profile_call


def test_the_target_is_stated_and_follows_from_the_rule():
    """60% of a core is the rule's figure; 6% a call is what it implies if
    a container is to carry a useful number of them."""
    assert profile_call.CORE_BUDGET == 0.60
    assert profile_call.TARGET_PER_CALL * 10 <= profile_call.CORE_BUDGET


def test_the_audio_is_the_shape_a_real_call_has():
    """Measured against synthetic audio in the wrong proportion is measured
    against a call nobody makes: a frame arriving while the agent speaks
    goes through the barge-in test as well, and costs more."""
    frames = profile_call.frames_for(10.0)
    assert len(frames) == 500
    speaking = sum(1 for frame in frames if frame != profile_call.SILENCE)
    assert abs(speaking / len(frames) - profile_call.CALLER_TALKS) < 0.1


def test_a_frame_is_twenty_milliseconds_at_the_capture_rate():
    assert profile_call.SAMPLES_PER_FRAME == 320
    assert len(profile_call.SILENCE) == 640


async def test_every_frame_is_actually_processed():
    """The bug this guards against produced a number ten times too good.

    `Conversation.run()` cancels whichever pump is still going when the
    other finishes, and the first version of the harness had a model that
    ran out of script first - so it profiled a fraction of the work and
    reported it as the whole.
    """
    import os

    from src.config import load

    os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
    cpu, frames = await profile_call.one_call(2.0, load("development"))
    assert frames == 100, "not every frame reached the session"
    assert cpu > 0
