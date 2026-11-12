"""What a shortlist is, and how "the second one" becomes a product.

Split out of `runner.py` because it answers a different question. The runner
decides WHICH lookups to make; this decides which product the customer meant,
and that is the part that goes wrong in a way the customer hears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.agent.tools.catalogue import Product

SHORTLIST = 3  # nobody can hold a longer list read aloud
# More than the shortlist, but only when there is a ceiling to drop against:
# the index's own price is sometimes wrong by enough to put a phone outside
# the budget it was found for, and those have to be droppable without leaving
# the customer with one option. With no budget nothing can be dropped, so
# pricing six to speak three is three upstream calls bought for nothing - and
# the slowest of six is what the caller waits for. Measured at 1,313ms for six
# against 502-568ms for the searches that had a budget.
CANDIDATES = 6

_WORDS = re.compile(r"[a-z0-9]+")
# Words that point at a product without naming one. Left in, "that one" would
# match any product whose name happens to contain "one".
_VAGUE = {
    "that",
    "this",
    "the",
    "one",
    "it",
    "ithu",
    "antha",
    "inthu",
    "atha",
    "first",
    "second",
    "third",
    "last",
    "phone",
    "mobile",
    "model",
}


@dataclass
class Candidate:
    ref: str
    item_code: str
    name: str
    price: int | None
    was: int | None
    brand: str | None
    in_stock_hint: bool
    specs: dict[str, str]
    rating: float | None


def candidate_from(
    ref: str, product: Product, price: int | None, was: int | None
) -> Candidate:
    return Candidate(
        ref=ref,
        item_code=product.item_code,
        name=product.name,
        price=price,
        was=was if was and was != price else None,
        brand=product.brand,
        in_stock_hint=product.in_stock,
        specs=dict(product.specs),
        rating=product.rating,
    )


def resolve(shortlist: list[Candidate], ref: Any) -> Candidate | None:
    """A ref into the shortlist, or a name the customer repeated back.

    A customer says "that Samsung" and "the second one", not "52064". The
    number is tried first because that is what the model is told to send; the
    words are a fallback for when it sends them anyway.

    Two candidates matching is not resolved by picking the first. One short
    question costs a turn; the wrong phone costs the sale.
    """
    if not shortlist:
        return None
    asked = str(ref or "").strip().lower()
    if not asked:
        # One candidate and no ref is not ambiguous.
        return shortlist[0] if len(shortlist) == 1 else None

    for candidate in shortlist:
        if asked == candidate.ref:
            return candidate

    words = {w for w in _WORDS.findall(asked) if w not in _VAGUE}
    matched = [c for c in shortlist if words & set(_WORDS.findall(c.name.lower()))]
    return matched[0] if len(matched) == 1 else None


def search_key(args: dict[str, Any]) -> str:
    """A search, as the thing it asks for rather than as the JSON it arrived in.

    The model sent {"category":"phone","query":"18 Pro"} and then
    {"query":"18 Pro","category":"phone"} - the same search twice, nine
    seconds apart, and a key built on the raw dict would have missed it
    because the keys came in a different order. So: sorted, lowercased,
    trimmed, and only the fields that change the answer.
    """
    parts = []
    for field in ("query", "category", "min_price", "max_price"):
        value = args.get(field)
        if value in (None, ""):
            continue
        parts.append(f"{field}={str(value).strip().lower()}")
    return "|".join(sorted(parts)) or "*"


def money(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def outside(price: int, high: int | None) -> bool:
    """Over the ceiling is a problem. Under the floor is a bargain.

    The ceiling is strict. A 5% margin here let a 30,999 phone through a
    20,000-30,000 question and the agent presented it as inside the range; a
    salesperson may offer to stretch a budget, but not silently.

    The floor is not a filter at all, and making it one was a worse bug than
    the one it was copied from. A customer asked for a Vivo between 25,000 and
    30,000, the index had exactly one, the price list said 22,999 - and it was
    dropped for being too cheap. They were told nothing was available. Nobody
    asking for a phone under 30,000 is disappointed by one at 22,999, and
    "innum kammi - 22,999" is a sentence that sells.
    """
    return high is not None and price > high
