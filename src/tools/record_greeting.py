"""Record the opening line once, so that no call ever generates it again.

`make greeting`. Costs a few paise, runs for about ten seconds, and writes
`src/agent/greeting.pcm`.

**Why this exists.** The greeting is fixed text - persona.py pins the wording
and the instruction tells the model to say it "exactly as written, in full".
Every call was nevertheless spending a whole generation producing it: on the
traced call of 2026-11-15 that was turn 1, Rs 0.45 of a Rs 5.46 bill, and
about half a second of silence before the customer heard anything. Paying a
model to read out a sentence that cannot vary is the clearest waste in the
call.

**Why it records rather than synthesising.** The recording has to be in the
same voice as everything the agent says afterwards, or the customer hears the
line handed from one speaker to another in the middle of a sentence. The only
way to be sure of that is to let the same model, through the same setup
message, say it - which is what this does. `GeminiLiveSession` is built here
exactly as `voice_call.py` builds it, so a change to the setup (a voice name,
a model, a sample rate) changes the live calls and this recorder together.
Re-run it after any such change: see the staleness note in `src/voice/opening.py`.

**What it does NOT do.** It never writes a file it did not fill. A short or
empty capture is a greeting that would cut off mid-word on every call from
then on, so it is refused and the old recording is left where it is.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from src.agent.persona import GREETING
from src.config import load
from src.voice.events import AudioOut, TurnComplete
from src.voice.gemini_live import GeminiLiveSession
from src.voice.opening import RECORDING, bytes_to_seconds

log = logging.getLogger(__name__)

# Below this the capture is a failure, not a short greeting. The line is two
# sentences and runs about five seconds; anything under two is a socket that
# closed early, and writing it would break every call that followed.
SHORTEST_CREDIBLE_SECONDS = 2.0

# Above this something is wrong in the other direction - the model answered
# rather than read, or the turn never ended. A caller should not be held for
# fifteen seconds before they can speak.
LONGEST_CREDIBLE_SECONDS = 15.0


def _wav(pcm: bytes, rate: int) -> bytes:
    """A playable file, for auditioning a voice before choosing it.

    The shipped recording is raw PCM on purpose - one consumer, which knows
    the rate from the config - but nothing plays raw PCM by double-clicking
    it, and a voice has to be heard to be chosen.
    """
    header = b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little")  # PCM header length
    header += (1).to_bytes(2, "little")  # uncompressed
    header += (1).to_bytes(2, "little")  # mono
    header += rate.to_bytes(4, "little")
    header += (rate * 2).to_bytes(4, "little")  # bytes per second
    header += (2).to_bytes(2, "little")  # block align
    header += (16).to_bytes(2, "little")  # bits per sample
    header += b"data" + len(pcm).to_bytes(4, "little")
    return header + pcm


async def record(destination: Path, voice: str = "", as_wav: bool = False) -> int:
    load_dotenv()
    config = load()
    if voice:
        config = replace(config, voice_name=voice)

    print(f"model {config.gemini_live_model}")
    print(f"voice {config.voice_name or '(the API default)'}")
    print(f"saying: {GREETING}\n")

    chunks: list[bytes] = []
    started = time.perf_counter()

    # Built the way voice_call.py builds it, minus the tools: the greeting
    # turn calls none, and declaring them would only add prompt tokens to a
    # request whose whole point is to be cheap.
    async with GeminiLiveSession(config) as model:
        await model.greet(GREETING)
        async for event in model.events():
            if isinstance(event, AudioOut):
                chunks.append(event.pcm)
            elif isinstance(event, TurnComplete):
                break

    pcm = b"".join(chunks)
    rate = config.playback_sample_rate
    seconds = bytes_to_seconds(pcm, rate)
    took = time.perf_counter() - started

    print(f"captured {len(pcm):,} bytes = {seconds:.2f}s at {rate} Hz in {took:.1f}s")

    if seconds < SHORTEST_CREDIBLE_SECONDS:
        print(
            f"\nREFUSED: {seconds:.2f}s is shorter than a greeting can be. "
            f"The socket probably closed early. Nothing was written."
        )
        return 1
    if seconds > LONGEST_CREDIBLE_SECONDS:
        print(
            f"\nREFUSED: {seconds:.2f}s is longer than a greeting should be. "
            f"The model answered instead of reading. Nothing was written."
        )
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_wav(pcm, rate) if as_wav else pcm)
    print(f"\nwrote {destination}")
    if as_wav or destination != RECORDING:
        # An audition, not the shipped recording. Saying otherwise sent a
        # reader looking for a behaviour change that had not happened.
        print("an audition copy. Nothing on a call changed.")
    else:
        print(
            "every call from now on plays this instead of generating it. "
            "Delete the file to go back to a generated greeting."
        )
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record the opening line once.")
    parser.add_argument(
        "--out",
        type=Path,
        default=RECORDING,
        help=f"where to write it (default {RECORDING})",
    )
    parser.add_argument(
        "--voice",
        default="",
        help="a prebuilt voice name, overriding VOICE_NAME for this recording",
    )
    parser.add_argument(
        "--wav",
        action="store_true",
        help="write a playable WAV instead of raw PCM - for auditioning only",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    return await record(args.out, voice=args.voice, as_wav=args.wav)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
