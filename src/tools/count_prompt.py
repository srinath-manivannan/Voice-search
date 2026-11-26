"""Count the configured standing instruction and tools with the text model.

Run `make prompt` to compare fixed input before and after a change. Live
usage also includes conversation history and provider framing. The Live
endpoint reports that count in each generation's usage metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from dotenv import load_dotenv

from src.agent.persona import system_instruction
from src.config import Config, load
from src.web.services.dependencies import declarations_for

HOST = "https://generativelanguage.googleapis.com/v1beta"

# The sections persona.py is written in. Used to split the instruction for
# reporting, so a cut shows up against the rule it came from.
HEADINGS = ("LANGUAGE", "HOW TO ANSWER", "TOOLS", "WHAT YOU MAY CLAIM", "ENDING")


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

    load_dotenv()
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

    tools = declarations_for(settings)
    declarations = tools["functionDeclarations"] if tools else []
    for declaration in declarations:
        size = await count(settings, declarations=[declaration])
        print(f"  {declaration['name']:24} {size:5}")

    fixed = await count(
        settings, instruction=instruction, declarations=declarations or None
    )
    print(f"  {'=' * 24} {'=' * 5}")
    print(f"  {'fixed input (text model)':24} {fixed:5}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
