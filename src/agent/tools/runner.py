"""Executing a tool call. One implementation, both surfaces.

Voice and chat ask for tools through completely different protocols - the
Live API's toolCall/toolResponse over a socket, and functionCall parts in an
HTTP body - but what a tool *does* must not differ between them. Two copies
would drift, and the drift would show up as the voice agent and the chat agent
giving different answers about the same phone.

Failures are reported as failures. "Nothing matched" and "the lookup did not
happen" are different things to tell a customer, and collapsing them into an
empty list makes the agent say the first when the second is true.

## Why this holds state now

Every service except discovery is keyed by an item code, so the conversation
that used to be one search per turn is really one search and then three
lookups:

    "30,000-kulla camera phone"   search          shortlist of three
    "second one price enna?"      no search       the code is already known
    "stock irukka?"               no search
    "EMI evalavu?"                no search

Three of four turns need no discovery at all, which is the whole latency
story. So the shortlist lives here for the length of the conversation and the
follow-up tools take a `ref` - "1", "2", "3" - into it.

A `ref` rather than the item code, for two reasons. The code must never be
spoken aloud, and a model handed one puts it in a sentence eventually. And
"the second one" is what a customer actually says, so the thing the model has
to resolve should be the thing the customer said.

## Why this session caches

Every tool call costs a whole extra generation. The model emits the call,
which is one turn that says nothing, and then answers, which is another - and
under the Live API's billing each of those re-processes the entire context
window. Measured on a real call: 17 times the caller spoke, 24 generations,
five of them silent, 413 thinking tokens spent saying nothing. That overhead
was about Rs 0.47 of every connected minute.

Three of those five tool calls were searches for a phone the session had
already found. So:

  - Pricing already asks the estate per item, and the estate takes a list of
    sections in one request. So it now asks for stock and offers in the SAME
    request and keeps them. "Stock irukka?" used to be impossible to answer
    without a round trip; now the answer is already in the agent's hands.
    That is the one change here that removes a generation outright.
  - An identical search is answered from the previous result, upstream
    untouched. Be honest about what this does and does not buy: the model has
    ALREADY spent a generation by the time the call reaches us, so this saves
    the upstream request and about two seconds of the customer's time, not
    the turn. What it also saves is correctness - it no longer rebuilds the
    shortlist, and a re-search used to renumber the refs under a customer who
    had been told "the first one" and "the second one".
  - Anything already fetched for an item is reused. Only EMI still goes
    upstream on demand, because it is a three-call chain keyed by a SKU the
    search does not carry.

Fewer tool calls is the rest of it, and that is the model's choice, not ours:
the descriptions and the persona now say the search result already holds
price, stock and offers and stays valid for the call. Worth about Rs 0.2 a
minute if it is obeyed, and measurable on the next trace rather than assumed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.agent.tools import estate as estate_api
from src.agent.tools.catalogue import Catalogue, CatalogueUnavailable, Product
from src.agent.tools.shortlist import (
    CANDIDATES,
    SHORTLIST,
    Candidate,
    candidate_from,
    money,
    outside,
    resolve,
    search_key,
)
from src.agent.tools.speech import WANTED, spoken, why

log = logging.getLogger(__name__)

# Fetched with the price, in the same request, for the items that survive into
# the shortlist. The estate takes a list of sections and answers them
# together, so this costs no extra round trip from here - and it turns the two
# commonest follow-up questions into something the agent can already answer.
#
# EMI is deliberately not here. It needs the `product` section for the SKU and
# then the calculator, which is a longer chain for a rarer question, and
# putting it on every search would buy latency nobody asked for.
WITH_PRICE: tuple[str, ...] = ("pricing", "stock", "offers")


class ToolSession:
    """The tools, for one conversation.

    Constructed per call and per chat, so the shortlist cannot leak between
    customers. Both surfaces build one and hand `run` to their adapter.
    """

    def __init__(
        self,
        catalogue: Catalogue | None,
        estate: estate_api.Backend | None = None,
    ) -> None:
        self._catalogue = catalogue
        self._estate = estate
        self._shortlist: list[Candidate] = []
        # What this conversation has already asked for. Both exist to stop a
        # second round trip for an answer we are already holding.
        self._searches: dict[str, dict[str, Any]] = {}
        # Keyed by item code, and carrying which sections were actually
        # asked for. Without that, a Facts fetched for price alone reports
        # EMI as "unknown" - which the agent is told means "nobody could
        # check" - and it would say so about something nobody ever asked.
        self._facts: dict[str, tuple[frozenset[str], estate_api.Facts]] = {}

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_products":
            return await self._search(args)
        if name == "product_details":
            return await self._details(args)
        log.warning("model asked for an unknown tool: %s", name)
        return {"error": "unknown_tool"}

    # ── discovery ─────────────────────────────────────────────────────────

    async def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._catalogue is None:
            return {"error": "catalogue_not_configured"}

        # The same search, asked again. Answer it from what we already have:
        # the upstream call is skipped, and - more important than the latency
        # - the shortlist is NOT rebuilt, so the refs the customer has been
        # told about keep pointing at the same phones.
        key = search_key(args)
        if (remembered := self._searches.get(key)) is not None:
            log.info("search already answered this call: %s", key)
            return remembered

        max_price = money(args.get("max_price"))
        min_price = money(args.get("min_price"))
        asked = str(args.get("query") or "").strip()
        # Only a ceiling can drop a candidate, so only a ceiling earns the
        # extra ones.
        limit = CANDIDATES if self._estate and max_price else SHORTLIST
        try:
            found = await self._catalogue.search(
                query=asked or "*",
                category=args.get("category"),
                min_price=min_price,
                max_price=max_price,
                limit=limit,
            )
            # "30,000 kulla camera phone" put "camera" in the query, which is
            # matched against product names and matches nothing - the index
            # states a camera on 2,397 of 12,926 products and never in the
            # name. The customer heard "we have none", for the commonest
            # question there is. A feature word is not a model name, so when
            # a query finds nothing and there is still a category or a budget
            # to search on, the words are dropped rather than the shelf.
            if found.found_nothing and asked and (args.get("category") or max_price):
                log.info("no name matched %r; searching the band instead", asked)
                found = await self._catalogue.search(
                    query="*",
                    category=args.get("category"),
                    min_price=min_price,
                    max_price=max_price,
                    limit=limit,
                )
        except CatalogueUnavailable as down:
            log.warning("catalogue unavailable: %s", down)
            return {"error": "catalogue_unavailable"}

        shortlist, priced, over = await self._price(found.products, max_price)
        self._shortlist = shortlist

        out: dict[str, Any] = {
            "total_matching": found.total,
            "products": [
                {
                    "ref": c.ref,
                    "name": c.name,
                    "price_inr": c.price,
                    "was_inr": c.was if c.was and c.was != c.price else None,
                    "brand": c.brand,
                    # The reason this phone is worth mentioning, and the only
                    # one the agent is allowed to give. Without it, asked why
                    # it suggested something, it made a camera up.
                    "specs": c.specs,
                    "rating_out_of_5": c.rating,
                    # Named a hint, not a promise: the feed can lag the shop
                    # even when both of its stock fields agree. Dropped below
                    # when the estate gave us the real thing.
                    "in_stock_hint": c.in_stock_hint,
                    **self._already_known(c),
                }
                for c in shortlist
            ],
        }
        if shortlist and not priced:
            # Only when there is a price to quote. The agent has to know the
            # difference, because an index price is a list price and sometimes
            # a stale one.
            out["price_note"] = "approximate - confirm before quoting"
        if over and not shortlist:
            # Everything matched and everything was dearer than they asked.
            # Saying only "nothing found" wastes the one thing we do know, and
            # "Samsung-la 25-30k-la ethuvum illa, 35,499-ku Galaxy A17 irukku"
            # is the answer a salesperson gives.
            out["nothing_inside_budget"] = True
            out["nearest_above_budget"] = over[0]
        self._searches[key] = out
        return out

    def _already_known(self, candidate: Candidate) -> dict[str, Any]:
        """Stock and offers for a shortlisted item, if pricing brought them.

        Short on purpose. These ride along so the agent can answer "stock
        irukka?" without another round trip, not so it can read a stock report
        out over three phones nobody asked about - and every extra field here
        is charged again on every turn of the call.
        """
        held = self._facts.get(candidate.item_code)
        if held is None:
            return {}
        facts = held[1]
        out: dict[str, Any] = {}
        if facts.stock.state == estate_api.KNOWN:
            # The real answer supersedes the index's guess, so the guess goes:
            # two stock fields disagreeing is how an agent contradicts itself.
            out["stock"] = {
                "state": estate_api.KNOWN,
                "available": facts.stock.in_stock,
            }
            out["in_stock_hint"] = None
        else:
            out["stock"] = why(facts.stock.state, facts.stock.reason)
        if facts.offers.state == estate_api.KNOWN and facts.offers.best_amount:
            best = facts.offers.items[0] if facts.offers.items else None
            out["best_offer"] = {
                "amount_inr": facts.offers.best_amount,
                "only_with": best.condition if best else None,
            }
        elif facts.offers.state == estate_api.NONE:
            out["best_offer"] = None
        return out

    async def _price(
        self, products: list[Product], max_price: int | None
    ) -> tuple[list[Candidate], bool, list[dict[str, Any]]]:
        """Replace index prices with the price in force, and drop the misfits.

        Measured on four phones the index offered inside 20,000-30,000: two of
        the four prices were wrong, one of them by 10,000 upward - a phone
        offered inside a 30,000 budget that actually sells at 35,499. The index
        carries the newest MRP record, not the price list, so every price the
        agent speaks is re-asked here and anything that leaves the budget is
        dropped rather than quoted.

        Without an estate configured this is a no-op and the caller says so.
        """
        estate = self._estate
        if estate is None:
            return (
                [
                    candidate_from(str(i + 1), p, p.price, None)
                    for i, p in enumerate(products)
                ],
                False,
                [],
            )

        async def selling(
            product: Product,
        ) -> tuple[Product, estate_api.Price | None]:
            # Stock and offers ride along with the price. The estate answers a
            # list of sections in one request, so this is the same round trip
            # it always was, and it saves the customer's next question a whole
            # generation of the model.
            try:
                facts = estate_api.read(
                    await estate.product(product.item_code, WITH_PRICE)
                )
            except estate_api.EstateUnavailable as down:
                log.info("pricing %s unavailable: %s", product.item_code, down)
                return product, None
            self._facts[product.item_code] = (frozenset(WITH_PRICE), facts)
            if facts.price.state != estate_api.KNOWN:
                return product, None
            return product, facts.price

        answered = await asyncio.gather(
            *(selling(p) for p in products), return_exceptions=False
        )

        kept: list[Candidate] = []
        over: list[dict[str, Any]] = []
        confirmed = 0
        for product, in_force in answered:
            price = in_force.selling if in_force else product.price
            if in_force:
                confirmed += 1
                if price is not None and outside(price, max_price):
                    log.info(
                        "dropping %s: price list says %s, over the ceiling",
                        product.item_code,
                        price,
                    )
                    over.append({"name": product.name, "price_inr": price})
                    continue
            # The former price is the price list's own MRP. The index's figure
            # is a different object and disagrees often enough that pairing it
            # with a price-list selling price produced "was 24,499, now
            # 30,999" - a rise, read aloud as a discount.
            kept.append(
                candidate_from(
                    str(len(kept) + 1),
                    product,
                    price,
                    in_force.list_price if in_force else None,
                )
            )
            if len(kept) == SHORTLIST:
                break

        over.sort(key=lambda o: o["price_inr"])
        # Every price spoken is a real one only if every survivor was answered.
        return kept, confirmed >= len(kept) and bool(kept), over

    # ── one item ──────────────────────────────────────────────────────────

    def _resolve_ref(self, ref: Any) -> Candidate | None:
        return resolve(self._shortlist, ref)

    async def _details(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._estate is None:
            return {"error": "details_not_configured"}
        picked = self._resolve_ref(args.get("ref"))
        if picked is None:
            return {
                "error": "no_such_product",
                "known_refs": [c.ref for c in self._shortlist],
            }

        want = [str(w).lower() for w in (args.get("want") or ["price"])]
        sections: tuple[str, ...] = tuple(
            dict.fromkeys(s for w in want for s in WANTED.get(w, ()))
        )
        if not sections:
            sections = WANTED["price"]

        # The search already fetched price, stock and offers for everything it
        # shortlisted. If that covers the question, answer from it: no upstream
        # call, and the customer hears the answer in the same breath.
        held = self._facts.get(picked.item_code)
        if held is not None and held[0].issuperset(sections):
            return spoken(held[1], picked, want)

        # Ask for what is missing together with what we would have asked for
        # anyway, so one more round trip answers everything about this item.
        if held is not None:
            sections = tuple(dict.fromkeys((*held[0], *sections)))
        try:
            facts = estate_api.read(
                await self._estate.product(picked.item_code, sections)
            )
        except estate_api.EstateUnavailable as down:
            log.warning("product details unavailable: %s", down)
            return {"error": "details_unavailable"}

        self._facts[picked.item_code] = (frozenset(sections), facts)
        return spoken(facts, picked, want)
