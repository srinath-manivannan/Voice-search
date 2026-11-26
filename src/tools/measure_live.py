"""What a Voice Call (Live) minute costs, measured the same way every time.

    .venv/bin/python -m src.tools.measure_live [ws://127.0.0.1:8080/ws]

Two fixed Tamil customer lines - a camera phone under ₹25,000, then "is the
first one in stock, and the EMI?" - are spoken into the real /ws socket at
real time, 20 ms frames, silence between, exactly as a browser microphone
sends them. The server's own cost lines (the ones the Voice Call screen
draws) are read back. Same input every run, so a lever's worth is the
difference between two runs, not an impression.

Costs money: one run is one real Live call, about ₹1.50-2. The caller lines
are synthesised once and kept in out/caller-lines.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import os
import time
from pathlib import Path
from typing import Any

import websockets
from websockets.typing import Origin
from dotenv import load_dotenv

from src.guided.speech import Speaker

LINES = (
    "எனக்கு ஒரு Samsung போன் வேணும், இருபத்தஞ்சாயிரத்துக்குள்ள, நல்ல கேமரா இருக்கணும்.",
    "முதல் போன் ஸ்டாக்ல இருக்கா? EMI எவ்வளவு?",
)
CALLER_VOICE = "ta-in-assistant-1"
STORE = Path("out/caller-lines")
RATE = 16_000
FRAME = RATE * 2 // 50  # 20 ms of 16-bit mono
SILENCE = b"\x00" * FRAME


async def caller_audio(api_key: str) -> list[bytes]:
    """Each line as 16 kHz PCM, synthesised once and then reused."""
    STORE.mkdir(parents=True, exist_ok=True)
    speaker = Speaker(api_key, voice=CALLER_VOICE)
    out = []
    for n, text in enumerate(LINES, 1):
        path = STORE / f"line{n}.pcm"
        if not path.exists():
            pcm, _ = await speaker.say(text)
            path.write_bytes(audioop.ratecv(pcm, 2, 1, 24_000, RATE, None)[0])
        out.append(path.read_bytes())
    return out


class Call:
    def __init__(self) -> None:
        self.last_cost: dict[str, Any] | None = None
        self.agent_audio_at = 0.0
        self.turns = 0
        self.replies = 0  # agent replies to a caller line, completed
        self.ended = False
        # What each side was heard saying, by turn, so a cheaper setting can
        # be checked for what it cost the answer and not only the rupees.
        self.said: dict[tuple[int, str], str] = {}


async def listen(socket: Any, call: Call) -> None:
    async for message in socket:
        if isinstance(message, bytes):
            call.agent_audio_at = time.monotonic()
            continue
        data = json.loads(message)
        if data.get("type") in ("transcript", "caller_transcript"):
            who = "agent" if data["type"] == "transcript" else "caller"
            key = (int(data.get("turn_id") or 0), who)
            call.said[key] = call.said.get(key, "") + str(data.get("text") or "")
        if data.get("type") == "turn":
            call.turns += 1
            if data.get("caller_reply"):
                call.replies += 1
            if data.get("cost"):
                call.last_cost = data["cost"]
        elif data.get("type") in ("summary", "cost") and data.get("cost"):
            call.last_cost = data["cost"]
        elif data.get("type") in ("ended", "error"):
            call.ended = True


async def speak(socket: Any, pcm: bytes) -> None:
    for start in range(0, len(pcm), FRAME):
        frame = pcm[start : start + FRAME].ljust(FRAME, b"\x00")
        await socket.send(frame)
        await asyncio.sleep(0.02)


async def quiet_until_agent_done(
    socket: Any, call: Call, *, settle: float, most: float, replies: int = 0
) -> None:
    """Keep the mic open (silence frames) until the agent has finished: the
    server has marked `replies` answers complete and it has been quiet a
    while since. Hanging up mid-answer under-counts the agent's voice."""
    began = time.monotonic()
    while time.monotonic() - began < most and not call.ended:
        await socket.send(SILENCE)
        await asyncio.sleep(0.02)
        heard_lately = time.monotonic() - call.agent_audio_at < settle
        answered = call.replies >= replies
        if answered and call.agent_audio_at > began and not heard_lately:
            return


async def run(url: str, origin: str) -> dict[str, Any]:
    load_dotenv(".env")
    lines = await caller_audio(os.environ["GEMINI_API_KEY"])
    call = Call()
    started = time.monotonic()
    async with websockets.connect(url, origin=Origin(origin), max_size=None) as socket:
        listener = asyncio.create_task(listen(socket, call))
        # The recorded opening plays first; the mic is open through it.
        await quiet_until_agent_done(socket, call, settle=1.5, most=15)
        for n, pcm in enumerate(lines, 1):
            await speak(socket, pcm)
            await quiet_until_agent_done(socket, call, settle=2.0, most=45, replies=n)
        connected = time.monotonic() - started
        await asyncio.sleep(0.5)
        listener.cancel()
    return {
        "seconds": round(connected, 1),
        "turns": call.turns,
        "cost": call.last_cost,
        "said": [
            f"{who}: {text.strip()}" for (_, who), text in sorted(call.said.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="ws://127.0.0.1:8080/ws")
    # The server allows only its own page's origin; a second test server on
    # another port still expects the usual one.
    parser.add_argument("--origin", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    result = asyncio.run(run(args.url, args.origin))
    cost = result["cost"] or {}
    lines = {line["key"]: line["inr_per_min"] for line in cost.get("lines", [])}
    total = round(sum(lines.values()), 2)
    print(
        f"{result['seconds']} s, {result['turns']} generations, "
        f"₹{total}/min  " + "  ".join(f"{k} {v:.2f}" for k, v in lines.items())
    )
    for line in result["said"]:
        print("   ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
