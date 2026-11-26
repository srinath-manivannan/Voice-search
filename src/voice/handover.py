"""What the next socket needs to know, in a few sentences of plain text.

The Live API carries its history as raw audio and re-bills every second of
it on every generation. Rotating onto a fresh socket throws that away - and
with it everything the agent knew, unless something is carried across.

This is that something. Measured 2026-11-12: 244 characters of text, and a
fresh session answered "which one was the second?", "what was my budget?"
and "what camera did the first one have?" correctly.

**Composed from state, never asked of the model.** `ToolSession` already
holds the shortlist, and the budget is an argument the model passed to
`search_products` - what it acted on, not what it said. Writing four
sentences from that is free, deterministic and cannot invent a price.
Asking the model to summarise itself costs a whole generation, and a
summary that hallucinates is worse than no summary, because the next
session believes it.

It is deliberately about the CALL, not about the products. Prices, stock and
offers are in `ToolSession` on our side and survive the rotation untouched;
re-stating them here would be a second copy to go stale, and the shortlist
refs still resolve because the shortlist itself never moved.
"""

from __future__ import annotations

from src.agent.tools.runner import ToolSession
from src.agent.tools.shortlist import Candidate

# A rotation the customer notices is a rotation that failed. The greeting is
# the loudest way to notice one, so it is refused explicitly rather than left
# to the persona's own instruction to behave.
CONTINUE = "Continue the call. Do not greet them again."


def _money(amount: int | None) -> str:
    return f"{amount:,}" if amount is not None else ""


def _budget_sentence(session: ToolSession) -> str | None:
    """The one thing compression could never keep.

    A budget is said once, at the start, which is exactly the turn a sliding
    window drops first. Eight turns into a probe the agent had lost it and
    offered a phone a third over. Here it is carried by construction.
    """
    if (budget := session.budget) is None:
        return None
    low, high = budget
    if low is not None and high is not None:
        return (
            f"They asked for something between {_money(low)} and {_money(high)} rupees."
        )
    if high is not None:
        return f"They asked for something under {_money(high)} rupees."
    return f"They asked for something over {_money(low)} rupees."


def _stock_words(session: ToolSession, candidate: Candidate) -> str:
    """What the old socket's search result said about stock, nothing more."""
    known = session.stock_for(candidate)
    if known is not None:
        return "in stock" if known else "out of stock"
    return "listed in stock" if candidate.in_stock_hint else "listed out of stock"


def _offered_sentence(session: ToolSession) -> str | None:
    """The shortlist, with the refs product_details takes and the stock.

    Without them, "is the first one in stock?" after a rotation made the
    agent run search_products again just to get back what the old socket
    already knew: one more generation, re-billed in full (measured
    2026-11-25, about ₹0.19 each time). Price and stock are the two facts
    the search result carried; anything else is product_details' to fetch.
    """
    if not session.shortlist:
        return None
    offered = "; ".join(
        f"ref {c.ref} {c.name}"
        + (f" at {_money(c.price)} rupees" if c.price else "")
        + f", {_stock_words(session, c)}"
        for c in session.shortlist
    )
    return (
        f"You offered {offered}. Those refs still work in product_details;"
        " do not search again for these."
    )


def _discussed_sentence(session: ToolSession) -> str | None:
    if (picked := session.discussed) is None:
        return None
    return f"They were asking about the {picked.name}."


def with_memory(session: ToolSession, memory: str) -> str:
    """The tool note, then Grok's, when a summary has landed.

    The tool note stays first because it is built from the shortlist and
    cannot invent a price. Grok may compress what was said. It may not
    overrule a price the tool note already stated, and an empty summary —
    memory off, or a call that failed — leaves this as exactly `note`.
    """
    base = note(session)
    extra = memory.strip()
    if not extra:
        return base
    if not base:
        return extra
    return f"{base}\n{extra}"


def note(session: ToolSession) -> str:
    """The carry-over, or an empty string when there is nothing to carry.

    Empty matters: rotating before the customer has said anything worth
    remembering should hand over nothing rather than a sentence claiming a
    conversation that did not happen.
    """
    sentences = [
        sentence
        for sentence in (
            _budget_sentence(session),
            _offered_sentence(session),
            _discussed_sentence(session),
        )
        if sentence
    ]
    if not sentences:
        return ""
    return " ".join(["Earlier in this same call:", *sentences, CONTINUE])
