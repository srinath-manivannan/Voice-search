"""Which per-item services this deployment can reach, and whether they answer.

Two different questions, and the gap between them is where this project has
lost the most time.

**Ready** means a host and a credential are configured. **Answers** means the
service accepted them. They came apart on production Central Inventory for
weeks: every section looked fine and stock came back `unknown` on every call,
because `CI_HOST` was unset and nothing on a running system said so. A
customer heard "ippo confirm panna mudiyathu" for every phone in the shop and
the agent was behaving correctly while doing it.

So `--live` exists, and it is the half worth running. `{"message":
"signature is invalid"}` arrives with **HTTP 200**, so a readiness check that
stops at configuration cannot tell a working token from a refused one.

    make estate                 what is configured, and what is not
    make estate ARGS=--live     ... and what the services actually say

Exits non-zero when a service for the selected environment is not ready, so
a deploy can run it as a gate.

No secret is ever printed. Hosts carrying a credential in the path are
redacted too, which is why some lines say `<redacted>` where a hostname would
be more useful - the PIM and offers hosts have their tokens in the URL.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

from dotenv import load_dotenv

from src.agent.tools import estate, estate_backends
from src.config import load

# Anything long enough to be a token. Deliberately greedy: a redacted
# hostname is a smaller problem than a key in a terminal someone screenshots.
SECRETISH = re.compile(r"[A-Za-z0-9_\-]{20,}")

# Both the documented example and a product that is reliably out of stock, so
# a run shows a `known` TRUE and a `known` FALSE rather than one of each kind
# of success. "Out of stock" and "could not ask" are the two the agent must
# never confuse, and this is where that is visible.
DEFAULT_ITEMS = ("54486", "48849")


def safe(value: Any) -> str:
    return SECRETISH.sub("<redacted>", str(value))


def readiness(settings: Any, backend: Any) -> int:
    """Print what each service would be called on. Returns services not ready."""
    print(f"backend  {settings.estate_backend}")
    print(f"api_env  {settings.estate_api_env}\n")

    secrets = settings.estate_secrets
    present = sorted(name for name, value in secrets.items() if value)
    absent = sorted(name for name, value in secrets.items() if not value)
    print(f"  set      {', '.join(present) or '(none)'}")
    print(f"  not set  {', '.join(absent) or '(none)'}\n")

    described = backend.describe()
    services = described.get("services") or {}
    not_ready = 0
    for name, detail in sorted(services.items()):
        if detail.get("ready"):
            print(f"  {name:9} ready    {safe(detail.get('host'))}")
            continue
        not_ready += 1
        print(f"  {name:9} NOT READY")
        if problem := detail.get("problem"):
            # The problem strings are long on purpose - they name the
            # variable to set and what to ask for - so they are wrapped
            # rather than cut.
            for line in _wrap(safe(problem), 66):
                print(f"            {line}")
    return not_ready


def _wrap(text: str, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


async def live(backend: Any, items: tuple[str, ...]) -> int:
    """Ask for real facts. Returns how many items left something unanswered."""
    print("\nlive, against the configured environment:")
    incomplete = 0
    for item in items:
        try:
            raw = await backend.product(
                item, (estate.PRICING, estate.STOCK, estate.OFFERS, estate.EMI)
            )
        except estate.EstateUnavailable as failed:
            incomplete += 1
            print(f"\n  item {item}: the whole lookup failed - {safe(failed)[:120]}")
            continue

        facts = estate.read(raw)
        print(f"\n  item {item}  ({raw.get('took_ms')} ms)")
        print(f"    price   {_price(facts.price)}")
        print(f"    stock   {_stock(facts.stock)}")
        print(f"    emi     {_emi(facts.emi)}")
        if facts.unanswered:
            incomplete += 1
            print(f"    could not ask: {', '.join(facts.unanswered)}")
    return incomplete


def _price(price: estate.Price) -> str:
    if price.state != estate.KNOWN:
        return f"{price.state} - {safe(price.reason or 'no reason given')}"
    return f"known, {price.selling:,}"


def _stock(stock: estate.Stock) -> str:
    if stock.state == estate.UNKNOWN:
        return f"UNKNOWN - {safe(stock.reason or 'no reason given')}"
    if stock.state == estate.NONE or stock.in_stock is False:
        # The distinction the whole module exists for: this is a real zero,
        # not a lookup that did not happen, and it is safe to say out loud.
        return "known, out of stock"
    return f"known, in stock - {stock.quantity} across {stock.branches} branches"


def _emi(emi: estate.Emi) -> str:
    if emi.state != estate.KNOWN:
        return f"{emi.state} - {safe(emi.reason or 'no reason given')}"
    return f"known, {emi.per_month:,}/month over {emi.months}"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="What the per-item estate can reach, and what it answers"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also call the services - the half a readiness check cannot do",
    )
    parser.add_argument(
        "--item",
        action="append",
        default=[],
        help=f"item code to look up with --live (default {', '.join(DEFAULT_ITEMS)})",
    )
    args = parser.parse_args()

    # `make prompt` does not do this and has to be run with the environment
    # already exported, which is a trap worth not repeating.
    load_dotenv()

    settings = load("development")
    backend = estate_backends.build(settings)
    if backend is None:
        print("No estate backend. Per-item facts are off entirely:")
        print("  ESTATE_BACKEND is off, or the Worker checkout is missing.")
        return 1
    if not hasattr(backend, "describe"):
        print(f"{type(backend).__name__} cannot describe itself; use --live.")
        return 0 if not args.live else await live(backend, tuple(args.item))

    not_ready = readiness(settings, backend)
    if args.live:
        items = tuple(args.item) or DEFAULT_ITEMS
        not_ready += await live(backend, items)
    return 1 if not_ready else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
