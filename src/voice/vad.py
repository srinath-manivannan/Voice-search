"""Energy voice-activity detection over 20 ms frames.

The server does its own turn detection, and Phase 0 showed it is good enough to
drive the conversation - it discards silence before billing and decides when the
caller has finished. So why have this at all?

Two jobs the server cannot do for us:

  Barge-in speed. Waiting for the server to notice the caller talking and tell
  us costs a network round trip. The agent keeps speaking over them for all of
  it. Detecting locally lets playback stop first and ask questions later.

  Latency measurement. "Time from end of caller speech to first audio" needs a
  local opinion about when speech ended, or there is nothing to measure from.

Deliberately not Silero or a learned model: this runs on every 20 ms frame of
every concurrent call, and an energy test with a tracked noise floor is cheap
and predictable. `VadFrame.is_speech` is the seam - a better detector can supply
the verdict later without the turn policy changing.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass


def frame_rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of one frame, 0.0 to ~32768."""
    if not pcm:
        return 0.0
    samples = array("h")
    # An odd trailing byte would raise; drop it rather than fail a live call.
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


@dataclass(frozen=True)
class VadFrame:
    rms: float
    noise_floor: float
    is_speech: bool


class EnergyVad:
    """Tracks the noise floor and calls frames speech or not.

    The floor decays upward slowly and drops to any quieter frame immediately,
    so a room that gets noisier is followed within a second or so while a brief
    quiet gap does not reset it. That asymmetry is the whole trick.
    """

    def __init__(
        self,
        speech_factor: float = 3.0,
        absolute_floor: float = 180.0,
        noise_rise_per_frame: float = 1.02,
        speech_frames_to_trigger: int = 3,
        silence_frames_to_release: int = 25,
    ) -> None:
        self._speech_factor = speech_factor
        self._absolute_floor = absolute_floor
        self._noise_rise = noise_rise_per_frame
        self._trigger = speech_frames_to_trigger
        self._release = silence_frames_to_release

        self._noise_floor = absolute_floor
        self._speech_run = 0
        self._silence_run = 0
        self._in_speech = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def accept(self, pcm: bytes) -> VadFrame:
        rms = frame_rms(pcm)

        loud_enough = rms > max(
            self._absolute_floor, self._noise_floor * self._speech_factor
        )

        if loud_enough:
            self._speech_run += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            self._speech_run = 0
            # Only learn the floor from frames that are not speech, or the
            # agent's own voice would train the detector to ignore the caller.
            if rms < self._noise_floor:
                self._noise_floor = max(rms, 1.0)
            else:
                self._noise_floor = min(self._noise_floor * self._noise_rise, rms)

        if not self._in_speech and self._speech_run >= self._trigger:
            self._in_speech = True
        elif self._in_speech and self._silence_run >= self._release:
            self._in_speech = False

        return VadFrame(
            rms=rms, noise_floor=self._noise_floor, is_speech=self._in_speech
        )
