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


def _offered_sentence(session: ToolSession) -> str | None:
    if not session.shortlist:
        return None
    offered = ", ".join(
        f"{c.name} at {_money(c.price)} rupees" if c.price else c.name
        for c in session.shortlist
    )
    return f"You offered {offered}."


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
