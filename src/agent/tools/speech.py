"""Turning what the estate answered into something that may be said aloud.

No codes, no field names, no JSON shape the customer could not follow if it
were read to them. Each part carries its own state, because "there is no
offer" and "we could not ask about offers" are different sentences, and
saying the first when the second is true is how a customer is told a discount
does not exist.
"""

from __future__ import annotations

from typing import Any

from src.agent.tools import estate as estate_api
from src.agent.tools.shortlist import Candidate

# What each thing the customer might ask needs fetched. Ordinary code, not the
# model's choice: a table is faster than a round trip and cannot invent an
# endpoint. EMI is the one with a dependency - the calculator is keyed by the
# SKU, which only PIM product has, and by the selling price, which only the
# price list has.
WANTED: dict[str, tuple[str, ...]] = {
    "price": ("pricing",),
    "stock": ("stock",),
    "offers": ("offers", "pricing"),
    "emi": ("product", "pricing", "emi"),
}


def why(state: str, reason: str | None) -> dict[str, Any]:
    """A state, and the reason only when there is one to give.

    `none` needs no reason - there is simply no offer. `unknown` always has
    one, and it is what tells the agent this is "I could not check" rather
    than "there is none".
    """
    return {"state": state, "reason": reason} if reason else {"state": state}


def spoken(
    facts: estate_api.Facts, picked: Candidate, want: list[str]
) -> dict[str, Any]:
    """What may be said about one item.

    `could_not_check` names the parts that did not answer, so the agent can
    say so instead of reporting an absence it never established.
    """
    out: dict[str, Any] = {"product": facts.name or picked.name}

    if "price" in want or facts.price.state == estate_api.KNOWN:
        out["price_inr"] = facts.price.selling
        if facts.price.list_price and facts.price.list_price != facts.price.selling:
            out["was_inr"] = facts.price.list_price
            out["discount_percent"] = facts.price.discount_percent

    if "stock" in want:
        stock = facts.stock
        out["stock"] = (
            {
                "state": stock.state,
                "available": stock.in_stock,
                "quantity": stock.quantity,
                "branches": stock.branches,
            }
            if stock.state == estate_api.KNOWN
            else why(stock.state, stock.reason)
        )

    if "offers" in want:
        offers = facts.offers
        out["offers"] = (
            {
                "state": offers.state,
                "best_inr": offers.best_amount,
                "price_after_best_offer_inr": offers.effective_price,
                # Stated by the price service itself: these are alternatives.
                "offers_stack": offers.stacks,
                "items": [
                    {
                        "label": o.label,
                        "amount_inr": o.amount,
                        "only_with": o.condition,
                    }
                    for o in offers.items
                ],
            }
            if offers.state == estate_api.KNOWN
            else why(offers.state, offers.reason)
        )

    if "emi" in want:
        emi = facts.emi
        out["emi"] = (
            {
                "state": emi.state,
                "from_inr_per_month": emi.per_month,
                "months": emi.months,
                "provider": emi.provider,
                "plan": emi.kind,
                "banks": emi.provider_count,
                "quoted_by": "the bank, through Poorvika's own calculator",
            }
            if emi.state == estate_api.KNOWN
            else why(emi.state, emi.reason)
        )

    if unanswered := [p for p in facts.unanswered if p in want or p == "pricing"]:
        out["could_not_check"] = unanswered
    return out
