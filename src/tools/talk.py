"""Hold a real conversation with the agent through this machine's microphone.

    .venv/bin/python -m src.tools.talk            # until Ctrl-C
    .venv/bin/python -m src.tools.talk --seconds 120

This is how Phase 1 gets measured. It prints per-turn first-audio latency and
token counts, then p50/p95 at the end, which are the numbers Phase 1 completes
on: p50 <= 1 s, p95 <= 2 s, reply length at or under the cap.

It costs money - every turn is a real Live API call. The cost line it prints
at the end is the single documented billing rule, not a pair of hypotheses:
Google re-processes and charges the whole context window on every turn. The
measurements that settled it are in ../BILLING-VERIFICATION.md and the pricing
is explained in docs/COST.md.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from dotenv import load_dotenv

from src.config import load
from src.voice.gemini_live import GeminiLiveSession
from src.voice.local_audio import MicrophoneSource, SpeakerSink
from src.voice.session import Conversation


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=float, default=None, help="stop after this long"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_dotenv()
    config = load()

    source = MicrophoneSource(config.capture_sample_rate)
    sink = SpeakerSink(config.playback_sample_rate)

    print(
        f"model {config.gemini_live_model} · reply cap {config.reply_word_cap} words "
        f"· thinking budget {config.thinking_budget}"
    )
    print("speak when ready. Ctrl-C to stop.\n")

    started = time.perf_counter()
    # One socket on purpose, where the browser path rotates. This harness
    # runs without tools, so `handover.note` would have nothing to carry and
    # a rotation would simply lose the conversation. It measures the model's
    # own latency; the browser path is where the cost of a real call is
    # measured. Do not "fix" this to match without giving it tools first.
    async with GeminiLiveSession(config) as model:
        conversation = Conversation(model, source, sink)
        runner = asyncio.create_task(conversation.run())
        try:
            if args.seconds:
                await asyncio.wait_for(asyncio.shield(runner), timeout=args.seconds)
            else:
                await runner
        except (asyncio.TimeoutError, KeyboardInterrupt):
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    await source.close()
    await sink.close()
    minutes = (time.perf_counter() - started) / 60

    result = conversation.result
    if not result.turns:
        print("no turns completed - did the microphone pick anything up?")
        return 1

    print(
        f"\n{'turn':>4} {'first audio':>12} {'response tok':>13} {'thinking tok':>13} {'barge-in':>9}"
    )
    for turn in result.turns:
        latency = f"{turn.first_audio_ms:.0f} ms" if turn.first_audio_ms else "-"
        print(
            f"{turn.turn_id:>4} {latency:>12} {turn.response_tokens:>13} "
            f"{turn.thinking_tokens:>13} {str(turn.barge_in):>9}"
        )

    latencies = result.latencies()
    if latencies:
        p50, p95 = percentile(latencies, 0.50), percentile(latencies, 0.95)
        print(
            f"\nfirst audio   p50 {p50:.0f} ms   p95 {p95:.0f} ms   (targets: 1000 / 2000)"
        )
        print(
            f"              {'PASS' if p50 <= 1000 and p95 <= 2000 else 'MISS'}  over {len(latencies)} turns"
        )

    cost = model.usage.breakdown(config.usd_inr, config.gst_rate, max(minutes, 1 / 60))
    print(f"\n{minutes:.1f} connected minutes, {len(result.turns)} turns")
    print(f"₹{cost.total_inr_per_min:.2f} per connected minute, incl GST", end="")
    print(f"   (ceiling ₹{config.max_inr_per_minute:.2f})")
    for line in cost.lines:
        if not line.tokens:
            continue
        print(
            f"  {line.label:<30} {line.tokens:>8,} tok"
            f"   ₹{line.inr_per_min:>5.2f}/min  {line.share * 100:>3.0f}%"
        )
    print(f"  {'-- total for the call':<30} {'':>12}   ₹{cost.total_inr:>5.2f}")
    print("  (model only - add telephony, hosting and rental for the all-in figure)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
