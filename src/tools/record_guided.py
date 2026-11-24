"""Record the guided call's fixed lines once, so that no session generates them.

`make guided-voice`. A few rupees, once, and writes one WAV per line of
`guided.script.SPOKEN` into `src/guided/recordings/`, with a manifest of the
text and the voice each was made from.

**One voice.** Lines are spoken by the speech model in `GUIDED_VOICE` - the
same model and voice that speak the sentences built from live data, so the
customer hears one speaker from the first question to the last price. They
used to be spoken by the Live model, whose voice cannot be handed a price
to say and be checked before anyone hears it.

**No instruction.** The text alone. A style instruction was tried, and on
an English line the model read the instruction aloud with the line.

**Only what changed.** A line whose recording was made from its current
text, in the current voice, is left alone.

**Nothing unheard is kept.** Each take is transcribed back and kept only if
what is heard is the line that was written; three takes, then the line goes
unrecorded and is shown in silence. What was heard is written to the
manifest beside what was meant.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from src.config import Config, load
from src.guided.listen import Ear, likeness
from src.guided.model import ModelUnavailable
from src.guided.script import SPOKEN
from src.guided.speech import RATE, Speaker
from src.guided.voice import RECORDINGS, read_manifest, wav, wav_seconds, write_manifest

log = logging.getLogger(__name__)

# The shortest line is three words and ran about a second and a half. Under
# this the socket closed early; over the other the model said more than it
# was given.
SHORTEST_SECONDS = 0.6
LONGEST_SECONDS = 12.0

# How alike what was heard must be to what was meant, 0 to 1, comparing
# letters only. Not 1.0: spoken Tamil is written several ways and the model
# transcribing it picks one - "பாக்கணும்" came back as "பார்க்கணும்", which
# scores 0.96 and is the same sentence. Not much lower either. The first run
# of this kept anything over 0.55, and kept two questions that had gained a
# "vanakkam" (0.84, 0.86) and a line heard as different words (0.60).
ALIKE_ENOUGH = 0.90
# A take that is not alike enough is tried again, because it is as likely
# that the listener misheard a one-second clip as that the speaker misread
# it, and a second take settles which. Three, then the line goes unrecorded.
TAKES = 3


def language_of(key: str) -> str:
    """Which language a line is in, read off its name: `ask_budget_en`."""
    return "en" if key.endswith("_en") else "ta"


@dataclass(frozen=True)
class Take:
    """One attempt at one line."""

    pcm: bytes
    seconds: float
    usd: float


def refusal(meant: str, heard: str | None, seconds: float) -> str | None:
    """Why this take must not be kept, or None if it may be."""
    if seconds < SHORTEST_SECONDS:
        return f"{seconds:.2f}s is too short - the socket probably closed early"
    if seconds > LONGEST_SECONDS:
        return f"{seconds:.2f}s is too long - the model said more than the line"
    if heard is None:
        return None  # not checked, by request
    if (alike := likeness(meant, heard)) < ALIKE_ENOUGH:
        return f"heard as something else (alike {alike:.2f})"
    return None


async def speak(config: Config, text: str) -> Take:
    """One line, in the guided call's voice, waiting out a busy model."""
    speaker = Speaker(config.gemini_api_key, config.guided_voice)
    for attempt in range(6):
        try:
            pcm, usd = await speaker.say(text)
            return Take(pcm, len(pcm) / (RATE * 2), usd)
        except ModelUnavailable as busy:
            if "429" not in str(busy):
                raise
            wait = 15 * (attempt + 1)
            print(f"           (the voice is busy - waiting {wait}s)")
            await asyncio.sleep(wait)
    raise ModelUnavailable("the voice stayed busy")


async def hear(config: Config, recording: bytes, lang: str = "ta") -> tuple[str, float]:
    """What the text model says this recording says, and what asking cost."""
    return await Ear(config.gemini_api_key, config.gemini_text_model).transcribe(
        recording, lang
    )


async def record(only: list[str], everything: bool, check: bool) -> int:
    load_dotenv()
    config = load()
    manifest = read_manifest()
    gross = config.usd_inr * (1 + config.gst_rate)
    print(f"voice {config.guided_voice}\n")

    wanted = {key: SPOKEN[key] for key in (only or SPOKEN)}
    spent, refused, made = 0.0, [], 0
    for key, text in wanted.items():
        path = RECORDINGS / f"{key}.wav"
        held = manifest.get(key) or {}
        current = held.get("text") == text and held.get("voice") == config.guided_voice
        if not everything and not only and current and path.is_file():
            print(f"  kept     {key}")
            continue

        recording, heard, seconds, why = b"", None, 0.0, "not attempted"
        for attempt in range(1, TAKES + 1):
            take = await speak(config, text)
            spent += take.usd
            recording, seconds, heard = wav(take.pcm, RATE), take.seconds, None
            if check and seconds >= SHORTEST_SECONDS:
                heard, cost = await hear(config, recording, language_of(key))
                spent += cost
            why = refusal(text, heard, seconds) or ""
            if not why:
                break
            print(f"  take {attempt}   {key}: {why}")
            print(f"           heard: {heard}")
        if why:
            print(f"  REFUSED  {key} after {TAKES} takes")
            print(f"           meant: {text}")
            refused.append(key)
            continue

        RECORDINGS.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recording)
        manifest[key] = {
            "text": text,
            "seconds": round(wav_seconds(recording) or 0.0, 2),
            "heard": heard,
            "voice": config.guided_voice,
        }
        # After every line, so a run that dies half-way keeps what it made.
        write_manifest(manifest)
        made += 1
        alike = f"{likeness(text, heard):.2f}" if heard is not None else "unchecked"
        print(f"  recorded {key}  {seconds:.2f}s  alike {alike}")
        print(f"           meant: {text}")
        print(f"           heard: {heard}")

    print(
        f"\n{made} recorded, {len(refused)} refused, cost Rs {spent * gross:.2f} with GST"
    )
    if refused:
        print("refused lines are shown without sound: " + ", ".join(refused))
        return 1
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record the guided call's fixed lines."
    )
    parser.add_argument(
        "--all", action="store_true", help="record every line again, current or not"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        choices=sorted(SPOKEN),
        help="record just this line (repeatable)",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="skip the read-back - keeps whatever was said, heard by nobody",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s %(message)s"
    )
    return await record(args.only, args.all, not args.no_check)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
