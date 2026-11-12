"""How much CPU one call costs, and how many a core can carry.

    .venv/bin/python -m src.tools.profile_call
    .venv/bin/python -m src.tools.profile_call --seconds 120 --profile

The rule asks for a CPU target verified by profiling. The target: **one
connected call must cost under 6% of a core**, so sixteen concurrent calls
leave a single-core container at or under the 60% the rule names. That is a
number to size a deployment with, and nothing here had one.

Costs nothing and touches nothing. The model is a fake that answers from
memory and the transports are in-process, so this measures OUR code - the
VAD, the gating, the framing, the turn policy - and not the network. That is
the right thing to measure: the network is not what a second concurrent call
contends for.

Audio is synthesised at the shape a real call has: 20 ms frames at 16 kHz,
speech for 42% of wall clock, which is what the measured call's microphone
gate actually forwarded.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
import math
import os
import pstats
import time
from array import array
from collections.abc import AsyncIterator

from src.config import Config, load
from src.voice.events import AudioOut, Event, TurnComplete
from src.voice.gemini_live import GeminiLiveSession
from src.voice.session import Conversation
from src.voice.usage import TurnUsage

FRAME_MS = 20
SAMPLE_RATE = 16000
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
# What the measured call's gate actually forwarded, and how long the agent
# held the floor. Both shape how much work a frame costs: a frame arriving
# while the agent speaks goes through the barge-in test as well.
CALLER_TALKS = 0.42
AGENT_TALKS = 0.63
# The rule's number, and what it means here.
CORE_BUDGET = 0.60
TARGET_PER_CALL = 0.06


def _speech_frame(index: int) -> bytes:
    """A frame loud enough to read as speech, cheap enough to make millions."""
    samples = array("h")
    for n in range(SAMPLES_PER_FRAME):
        # Two tones and a wobble, so the RMS is speech-like rather than a
        # constant a branch predictor would learn.
        t = (index * SAMPLES_PER_FRAME + n) / SAMPLE_RATE
        value = 6000 * math.sin(2 * math.pi * 180 * t) + 2500 * math.sin(
            2 * math.pi * 410 * t
        )
        samples.append(int(value))
    return samples.tobytes()


SILENCE = bytes(SAMPLES_PER_FRAME * 2)


async def _deaf() -> Deaf:
    return Deaf()


def frames_for(seconds: float) -> list[bytes]:
    """A call's worth of audio, in the proportion a real one has."""
    total = int(seconds * 1000 / FRAME_MS)
    speech = _speech_frame(0)
    # Bursts rather than an even sprinkle: the VAD tracks a noise floor and
    # latches, so alternating every frame would measure a situation that
    # never happens.
    burst = int(1.5 * 1000 / FRAME_MS)
    out: list[bytes] = []
    while len(out) < total:
        out += [speech] * min(burst, total - len(out))
        quiet = int(burst * (1 - CALLER_TALKS) / CALLER_TALKS)
        out += [SILENCE] * min(quiet, max(total - len(out), 0))
    return out[:total]


class Recorded:
    """A source that yields prepared frames as fast as they are taken."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        # Counted, because the first version of this measured a fraction of
        # them: the model pump finished its script first and `run()` cancels
        # whichever pump is still going. A profile of a tenth of the work
        # reads as a service that is ten times cheaper than it is.
        self.delivered = 0

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    async def frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            self.delivered += 1
            yield frame

    async def close(self) -> None:
        return None


class Discard:
    """A sink that does what a speaker does, minus the speaker."""

    def __init__(self) -> None:
        self.written = 0

    @property
    def sample_rate(self) -> int:
        return 24000

    async def write(self, pcm: bytes) -> None:
        self.written += len(pcm)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class Deaf:
    """A socket that accepts everything and answers the handshake.

    `GeminiLiveSession` takes its `connect` injected so it can be driven
    without a network - this uses that seam. The point is that the REAL
    adapter does the real per-frame work: base64 of 640 bytes and a JSON
    envelope, fifty times a second, which the first version of this harness
    skipped entirely by faking the model instead of the socket.
    """

    def __init__(self) -> None:
        self._replies = [json.dumps({"setupComplete": {}})]

    async def send(self, data: str) -> None:
        return None

    async def recv(self) -> str:
        if self._replies:
            return self._replies.pop(0)
        await asyncio.sleep(3600)
        return ""

    async def close(self) -> None:
        return None


class Answering:
    """A model that replies from memory: no socket, no key, no money.

    Driven by the caller rather than by a script, and it never ends on its
    own. `Conversation.run()` cancels whichever pump is still going when the
    other finishes, so a model that runs out first silently truncates the
    thing being measured.
    """

    def __init__(self, seconds: float, real: GeminiLiveSession | None = None) -> None:
        self._turn = 0
        self._asked: asyncio.Queue[int] = asyncio.Queue()
        self._reply = bytes(4096)
        # The real adapter, when there is one: every frame then goes through
        # the same base64 and JSON it would on a call.
        self._real = real

    @property
    def turn_id(self) -> int:
        return self._turn

    async def send_audio(self, pcm: bytes) -> None:
        if self._real is not None:
            await self._real.send_audio(pcm)

    async def end_of_speech(self) -> None:
        self._turn += 1
        self._asked.put_nowait(self._turn)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            turn = await self._asked.get()
            # One reply, in the chunks the real adapter yields. Roughly the
            # seven seconds of speech a measured reply ran to.
            for _ in range(40):
                yield AudioOut(turn_id=turn, pcm=self._reply)
                await asyncio.sleep(0)
            yield TurnComplete(turn_id=turn, usage=TurnUsage(), first_audio_ms=500.0)


async def one_call(seconds: float, settings: Config) -> tuple[float, int]:
    """Run a call's audio through the real session. Returns (cpu, frames)."""
    source = Recorded(frames_for(seconds))
    adapter = GeminiLiveSession(settings, connect=lambda url: _deaf())
    await adapter.__aenter__()
    conversation = Conversation(
        Answering(seconds, adapter), source, Discard(), agent_opens=False
    )
    started = time.process_time()
    await conversation.run()
    cpu = time.process_time() - started
    if source.delivered < len(source._frames):
        raise SystemExit(
            f"only {source.delivered} of {len(source._frames)} frames were "
            "processed - the measurement would be a fraction of the truth"
        )
    return cpu, source.delivered


def report(cpu: float, frames: int, seconds: float) -> None:
    per_frame_us = cpu / frames * 1e6
    share = cpu / seconds
    concurrent = int(CORE_BUDGET / share) if share else 0
    print(f"  audio simulated        {seconds:.0f} s   {frames} frames")
    print(f"  CPU consumed           {cpu * 1000:.0f} ms")
    print(f"  per 20 ms frame        {per_frame_us:.1f} us")
    print(f"  one call costs         {share * 100:.2f}% of a core")
    print(f"  target                 under {TARGET_PER_CALL * 100:.0f}%")
    print(f"  verdict                {'OK' if share <= TARGET_PER_CALL else 'OVER'}")
    print(f"  calls per core at 60%  {concurrent}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=180.0)
    parser.add_argument(
        "--profile", action="store_true", help="also print the hot functions"
    )
    args = parser.parse_args()

    # A key is required to construct the adapter and is never used: the
    # socket is a fake and nothing is sent anywhere.
    os.environ.setdefault("GEMINI_API_KEY", "profiling-only-never-sent")
    settings = load("development")

    print(f"profiling {args.seconds:.0f} s of call audio, no network\n")
    cpu, frames = await one_call(args.seconds, settings)
    report(cpu, frames, args.seconds)

    if args.profile:
        print("\n  where it goes:\n")
        profiler = cProfile.Profile()
        profiler.enable()
        await one_call(min(args.seconds, 60.0), settings)
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(12)
        for line in stream.getvalue().splitlines():
            if line.strip():
                print("   " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
