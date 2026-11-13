"""How big the standing prompt is, section by section.

    make prompt

The system instruction and the tool declarations are charged on EVERY
generation of every call, and sliding-window compression is documented to keep
them: "System instructions ... will always remain at the beginning of the
result." So this is the one number in the project that is paid for about eight
times a minute, forever, and it had never been measurable without placing a
call.

Costs nothing. `countTokens` is not billed.

TWO THINGS THIS IS NOT

**It is not the Live model's count.** `gemini-3.8-live` refuses countTokens
outright - "only supports real-time bidirectional streaming" - so this counts
with the text model instead. On the last measured call the Live model reported
a 1,430-token floor where this counts 1,230, so it reads about 14% low. The
ratio is printed beside the figure rather than folded into it, because a
correction factor that hides inside a number is one nobody can check.

**It is not a budget.** It is an instrument. What it is for is the second
column: a cut you cannot measure is a cut you cannot defend, and the first
pass over persona.py was argued entirely in prose.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from src.agent.persona import system_instruction
from src.agent.tools.declarations import PRODUCT_DETAILS, SEARCH_PRODUCTS
from src.config import Config, load

HOST = "https://generativelanguage.googleapis.com/v1beta"

# The sections persona.py is written in. Used to split the instruction for
# reporting, so a cut shows up against the rule it came from.
HEADINGS = ("LANGUAGE", "HOW TO ANSWER", "TOOLS", "WHAT YOU MAY CLAIM")

# What the Live model reported as its prompt_text floor on the measured call
# of 2026-11-10, against what this counted for the same text. Printed, never
# applied. See docs/COST.md.
LIVE_FLOOR, TEXT_COUNT_THEN = 1430, 1230


async def count(
    settings: Config,
    *,
    instruction: str | None = None,
    declarations: list[dict[str, Any]] | None = None,
) -> int:
    """Tokens for this instruction and these declarations, minus the probe.

    The key goes in a header. It is a secret and a URL is not a private
    place: query strings reach access logs, proxy logs and exception text.
    """
    import httpx

    request: dict[str, Any] = {
        "model": f"models/{settings.gemini_text_model}",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    }
    if instruction is not None:
        request["systemInstruction"] = {"parts": [{"text": instruction}]}
    if declarations is not None:
        request["tools"] = [{"functionDeclarations": declarations}]

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{HOST}/models/{settings.gemini_text_model}:countTokens",
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            json={"generateContentRequest": request},
        )
        if response.status_code != 200:
            # Never the body: an error body is allowed to echo a header back.
            raise SystemExit(f"countTokens returned {response.status_code}")
        total: int = response.json()["totalTokens"]
    # The two-token probe content is not part of the standing prompt.
    return total - 2


def sections(text: str) -> dict[str, str]:
    """Split the instruction at its own headings, preserving every line."""
    blocks: dict[str, str] = {}
    current: list[str] = []
    name = "(opening)"
    for line in text.split("\n"):
        if line.strip() in HEADINGS:
            blocks[name] = "\n".join(current)
            current, name = [], line.strip()
        else:
            current.append(line)
    blocks[name] = "\n".join(current)
    return blocks


async def main() -> int:
    parser = argparse.ArgumentParser(description="Size of the standing prompt")
    parser.add_argument("--surface", default="voice", choices=("voice", "chat"))
    args = parser.parse_args()

    settings = load("development")
    cap = settings.reply_word_cap if args.surface == "voice" else settings.chat_word_cap
    instruction = system_instruction(args.surface, cap)

    print(f"counted with {settings.gemini_text_model}, {args.surface} surface\n")

    total = 0
    for label, body in sections(instruction).items():
        size = await count(settings, instruction=body)
        total += size
        print(f"  {label:24} {size:5}")
    print(f"  {'-' * 24} {'-' * 5}")

    whole = await count(settings, instruction=instruction)
    print(f"  {'system instruction':24} {whole:5}   (sections sum to {total})")

    for label, declaration in (
        ("search_products", SEARCH_PRODUCTS),
        ("product_details", PRODUCT_DETAILS),
    ):
        print(f"  {label:24} {await count(settings, declarations=[declaration]):5}")

    fixed = await count(
        settings,
        instruction=instruction,
        declarations=[SEARCH_PRODUCTS, PRODUCT_DETAILS],
    )
    print(f"  {'=' * 24} {'=' * 5}")
    print(f"  {'charged every turn':24} {fixed:5}")

    scaled = fixed * LIVE_FLOOR / TEXT_COUNT_THEN
    print(
        f"\n  the Live model counted {LIVE_FLOOR} where this counted "
        f"{TEXT_COUNT_THEN}, so expect about {scaled:.0f} on a call."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
