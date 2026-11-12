"""Per-item facts: the price in force, live stock, card offers and bank EMI.

The catalogue answers "which phones". It cannot answer "what does this one
cost today, is it on a shelf, and what will my card take off it" - those live
in PIM, Central Inventory and the offers rule engine, and the index holds a
copy that is right until it is not. Measured on 2026-11-04, on four phones the
index itself offered inside a 20,000-30,000 budget:

    item    index price   production list   production selling
    45902        29,999            29,999               22,999
    48652        25,999            25,999               21,999
    30968        27,000            10,990               10,990
    52064        25,499            36,499               35,499

Two of the four are wrong, one by 10,000 in the direction that matters: a
phone offered inside a 30,000 budget actually sells at 35,499. The index's
`price` is the newest MRP record and its `mrp` field is a dated history, so it
is a list price and sometimes a stale one. It is used to find candidates and
never to quote a price.

It also carries no selling price at all, and the EMI calculator is keyed by
the selling price - quoting the list price instead put a MacBook's headline
EMI at Rs 17,568 against the shop's own Rs 16,690. So an EMI answer is only
possible through this module.

## Why this calls the Worker's code instead of its own

Search Blocks already built this: `product.py` plans which services a question
needs, fans them out under one deadline, and reads each body through
`adapters.py`, which knows the traps. Three of those traps cost real money to
find and none of them are guessable:

  * `{"message": "signature is invalid"}` arrives with **HTTP 200**. Read as
    zero stock, it puts a Notify Me button on a phone that is on the shelf.
  * EMI is keyed by the **SKU** and the selling price **in paise**, and is the
    CRM calculator the shop's own page calls - not the NBFC endpoint, which
    404s on poorvika.com too.
  * "the smallest instalment wins" is wrong. A No Cost plan over 6 months has
    the largest instalment of the three kinds and is the cheapest offer on the
    page, so each kind keeps its own cheapest and the headline is Standard.

Reimplementing that would re-earn every one of those bugs, so there are two
backends behind one method and no second copy of the logic:

  `WorkerEstate`      POST /product on the deployed Worker. The production
                      path. Needs the route deployed; the Worker at
                      dev-search-proxy predates it and answers "q is required".
  `InProcessEstate`   the same `product.py`, `apis.py` and `adapters.py`
                      imported unchanged and run here, with `js.fetch` shimmed
                      onto httpx. This is what `product_probe.py` does, and it
                      is how every number in this docstring was measured.

## One environment, never two

`apis.host()` refuses a stage host under `API_ENV=production` and the reverse,
because the failure worth designing against is a mixed answer - production
pricing spoken beside staging stock, both equally confident. That leaves a
real gap rather than a hidden one:

    stage        stock OK           EMI not_configured  (no staging twin)
    production   EMI + offers OK    stock not_configured (no working token)

Production Central Inventory does answer, behind the storefront, with an
authorization the storefront holds and we do not; the published "production"
token is staging's and is refused there under every mode. So stock is
answered on stage and reported honestly as unknown on production, and
`UNKNOWN` is never spoken as "none".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # importing it at runtime would close a cycle through config
    from src.config import Config

log = logging.getLogger(__name__)

# What a section's status means to someone speaking to a customer. The upstream
# has seven; only the first two carry data, and collapsing the rest into
# "there is none" is the one error here a customer can see.
KNOWN = "known"  # answered, and there is something to say
NONE = "none"  # answered, and the answer is genuinely nothing
UNKNOWN = "unknown"  # we could not ask, so we must not claim either way

_SPOKEN = {"ok": KNOWN, "empty": NONE}

# Sections, named as the Worker names them.
PRICING = "pricing"
STOCK = "stock"
OFFERS = "offers"
EMI = "emi"
PRODUCT = "product"


class EstateUnavailable(RuntimeError):
    """The whole lookup failed, not one section of it.

    A single section that did not answer is normal and is reported in place.
    This is for the case where nothing was asked at all - no backend, no
    network - because that is the one the agent must not describe as a result.
    """


@dataclass(frozen=True)
class Stock:
    state: str
    in_stock: bool | None = None
    quantity: int | None = None
    branches: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Emi:
    state: str
    per_month: int | None = None
    months: int | None = None
    provider: str | None = None
    kind: str | None = None
    provider_count: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Offer:
    """One card offer, with the condition that makes it apply.

    `condition` is not decoration. "HDFC card-la 2,000 off irukku" is true and
    "2,000 off irukku" is not, and a customer who hears the second quotes it
    back at the counter.
    """

    label: str
    amount: int | None
    condition: str | None
    lowers_price: bool


@dataclass(frozen=True)
class Offers:
    state: str
    items: list[Offer] = field(default_factory=list)
    best_amount: int | None = None
    effective_price: int | None = None
    reason: str | None = None
    # The upstream states this itself, and it is the answer to "so that is
    # 2,000 plus 1,500 off?" - no. They are alternatives.
    stacks: bool = False


@dataclass(frozen=True)
class Price:
    state: str
    selling: int | None = None
    list_price: int | None = None
    discount_percent: int | None = None
    inclusive_of_taxes: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class Facts:
    """One item, as much of it as answered."""

    item_code: str
    name: str | None = None
    price: Price = field(default_factory=lambda: Price(UNKNOWN))
    stock: Stock = field(default_factory=lambda: Stock(UNKNOWN))
    offers: Offers = field(default_factory=lambda: Offers(UNKNOWN))
    emi: Emi = field(default_factory=lambda: Emi(UNKNOWN))
    took_ms: int = 0

    @property
    def unanswered(self) -> tuple[str, ...]:
        """Which of the four could not be asked. Spoken as "I cannot confirm"."""
        return tuple(
            name
            for name, part in (
                (PRICING, self.price),
                (STOCK, self.stock),
                (OFFERS, self.offers),
                (EMI, self.emi),
            )
            if part.state == UNKNOWN
        )


class Backend(Protocol):
    """The one call. Both backends return the Worker's own response shape."""

    async def product(
        self, item_code: str, sections: tuple[str, ...]
    ) -> dict[str, Any]: ...


def _state(section: dict[str, Any] | None) -> tuple[str, dict[str, Any], str | None]:
    """(state, data, reason) for one section of the Worker's response."""
    if not isinstance(section, dict):
        return UNKNOWN, {}, "that section was not asked for"
    state = _SPOKEN.get(str(section.get("status") or ""), UNKNOWN)
    data = section.get("data")
    return state, data if isinstance(data, dict) else {}, section.get("reason")


def _int(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number))


def read(payload: dict[str, Any]) -> Facts:
    """The Worker's response, turned into what may be said out loud.

    Deliberately lossy. The response carries branch-level quantities, every
    tenure of all twenty card providers, the interest the bank will charge and
    a thousand words of terms - none of which belongs in a sentence read down
    a phone line.

    Values are taken from the derived `summary` where it has them, because
    those are the two numbers poorvika.com itself prints and they were checked
    against the shop on four products. The per-section `status` still decides
    whether anything may be said at all: summary carries no statuses, so
    reading it alone would turn a service that did not answer into a confident
    silence.
    """
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        raise EstateUnavailable("no sections in the product response")
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}

    return Facts(
        item_code=str(payload.get("item_code") or ""),
        name=_name(sections),
        price=_price(sections, summary),
        stock=_stock(sections),
        offers=_offers(sections, summary),
        emi=_emi(sections, summary),
        took_ms=_int(payload.get("took_ms")) or 0,
    )


def _name(sections: dict[str, Any]) -> str | None:
    state, data, _ = _state(sections.get(PRODUCT))
    name = str(data.get("name") or "").strip() if state == KNOWN else ""
    return name or None


def _price(sections: dict[str, Any], summary: dict[str, Any]) -> Price:
    state, data, reason = _state(sections.get(PRICING))
    if state != KNOWN:
        return Price(state, reason=reason)
    # PIM can mark a price as one the shop does not display. Speaking it anyway
    # would publish a number Poorvika chose to withhold.
    if data.get("hide_selling_price"):
        return Price(UNKNOWN, reason="this product's price is not for display")
    # `list_price` in the summary is the price list's own figure, which on a
    # discounted product equals the selling price - using it as the former
    # price silently hid every discount. The MRP is the number the shop
    # strikes through.
    return Price(
        KNOWN,
        selling=_int(summary.get("selling_price") or data.get("price")),
        list_price=_int(summary.get("mrp") or data.get("mrp")),
        discount_percent=_int(
            summary.get("discount_percent") or data.get("discount_percent")
        ),
        inclusive_of_taxes=bool(summary.get("inclusive_of_taxes", True)),
    )


def _stock(sections: dict[str, Any]) -> Stock:
    state, data, reason = _state(sections.get(STOCK))
    if state != KNOWN:
        return Stock(state, reason=reason)
    return Stock(
        KNOWN,
        in_stock=bool(data.get("in_stock")),
        quantity=_int(data.get("quantity")),
        branches=_int(data.get("branch_count")),
    )


def _condition(offer: dict[str, Any]) -> str | None:
    """What a customer must pay with for this offer to apply."""
    payment = offer.get("payment")
    payment = payment if isinstance(payment, dict) else {}
    cards = [str(c) for c in (payment.get("card_types") or [])]
    kinds = [str(t) for t in (payment.get("transaction_types") or [])]
    parts = []
    if cards:
        parts.append("/".join(cards) + " card")
    if kinds:
        parts.append("on " + "/".join(kinds))
    return " ".join(parts) or None


def _offers(sections: dict[str, Any], summary: dict[str, Any]) -> Offers:
    state, data, reason = _state(sections.get(OFFERS))
    if state != KNOWN:
        return Offers(state, reason=reason)

    found: list[Offer] = []
    for offer in data.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        amount = _int(offer.get("amount"))
        # An EMI-availability row is not a discount - it arrives with a null
        # amount and a 0% "discount", and reading it as money off is how an
        # agent invents a saving.
        if not amount and not offer.get("reduces_effective"):
            continue
        # Supplier-authored text on its way into a prompt. Capped, and data.
        label = str(offer.get("label") or offer.get("kind") or "offer")[:120]
        found.append(
            Offer(
                label=label.strip(),
                amount=amount,
                condition=_condition(offer),
                lowers_price=bool(offer.get("reduces_effective")),
            )
        )

    if not found:
        return Offers(NONE)
    best = summary.get("best_offer")
    best = best if isinstance(best, dict) else {}
    return Offers(
        KNOWN,
        items=found,
        best_amount=_int(best.get("amount"))
        or max((o.amount or 0 for o in found), default=0)
        or None,
        effective_price=_int(summary.get("effective_price")),
    )


def _emi(sections: dict[str, Any], summary: dict[str, Any]) -> Emi:
    state, data, reason = _state(sections.get(EMI))
    if state != KNOWN:
        return Emi(state, reason=reason)
    headline = summary.get("emi_starts_from") or data.get("headline")
    if not isinstance(headline, dict):
        return Emi(UNKNOWN, reason="EMI answered without a headline plan")
    return Emi(
        KNOWN,
        per_month=_int(headline.get("per_month")),
        months=_int(headline.get("months")),
        provider=str(headline.get("provider") or "").strip() or None,
        kind=str(headline.get("type") or "").strip() or None,
        provider_count=_int(data.get("provider_count")),
    )


def build(config: Config) -> Backend | None:
    """The backend this deployment should use, or None to run without one.

    Returning None is a supported state, not a failure: the agent still finds
    products and still quotes the catalogue's own price, and says that stock
    and EMI could not be checked. That is a worse agent and an honest one.

    The import is inside the function on purpose. `estate_backends` needs the
    shapes defined above it, and this is the one call that needs the
    backends - a module-level import would make the pair circular for the
    sake of a function nothing calls in a hot path.
    """
    from src.agent.tools.estate_backends import build as make

    return make(config)
