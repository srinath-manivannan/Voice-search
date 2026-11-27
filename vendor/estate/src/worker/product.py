"""One product, answered from the four services that actually know.

The search index is for discovery. It is a copy, and a copy is the wrong
thing to quote a shopper on a page where they are deciding: the price, the
stock and the offers live in PIM, Central Inventory and the rule engine, and
only those four are asked here.

The shape of the work is fixed by one fact — **every one of these endpoints
is per item**. A results grid of 40 products cannot be enriched; a product
view, one item and one interested shopper, can. So this module does one
item (or a short compare list) and refuses to be used as a grid.

What it does, in order:

    item_code        from the caller, or resolved from a query
    plan             which sections this question needs, and no more
    fan out          every section at once, one shared deadline
    adapt            each body read by `adapters`, which knows the traps
    assemble         per-section status, so one failure costs one section

Nothing here asks a language model anything. The model reads the result;
it does not decide which calls to make.
"""
from __future__ import annotations

import asyncio
import datetime
import re
import time
from urllib.parse import quote

import adapters
import apis

# Section -> the calls it needs. A caller asks for sections, never for
# endpoints, so an endpoint can be added to a section without the callers
# changing.
SECTIONS = ("product", "pricing", "stock", "offers", "finance", "emi",
            "policy", "delivery", "together", "related")

# What only a product page shows. A comparison lines several products up and
# has no use for each one's delivery quote or its neighbours' shelves.
PAGE_ONLY = ("delivery", "together", "related")

# Fetched only when named, never by default: the live stock of every sibling
# variant, for the chooser's "out of stock" marks. It is one Central Inventory
# call per sibling — ten for an Apple Watch — so the page asks for it after it
# has painted rather than making the shopper wait on it, and nobody else pays.
OPT_IN = ("siblings", "reviews")

# The figures an unlisted item's summary does not carry: everything that is a
# price. The shop prints none of them for a listing that is switched off.
UNLISTED_HIDES = ("selling_price", "list_price", "mrp", "discount_percent",
                  "activation_discount", "activation_offers", "effective_price",
                  "best_offer", "card_offers", "offers_considered",
                  "other_channel", "hide_selling_price")
SIBLINGS_MAX = 16
SIBLINGS_IN_FLIGHT = 6

# Where a delivery quote is for when the shopper has not said: the shop's own
# default, "Delivery to Chennai", pincode 600001.
DEFAULT_PINCODE = "600001"
# ASCII digits only: Python's \d also matches "६००००१" and other scripts.
_PINCODE = re.compile(r"[1-9][0-9]{5}")

# What a plain question needs. Deliberately small and deliberately literal:
# a planner that guesses is a planner that fetches five services to answer
# "what does it cost". Anything unmatched gets the overview.
INTENTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("price", "cost", "rate", "how much", "mrp", "discount percent"),
     ("pricing",)),
    (("stock", "available", "availability", "in stock", "delivery",
      "deliver", "pincode"), ("stock", "delivery")),
    (("bought together", "accessories", "goes with"), ("together",)),
    (("related", "similar", "alternative", "alternatives"), ("related",)),
    (("offer", "offers", "bank", "cashback", "instant", "coupon"),
     ("offers", "pricing")),
    (("emi", "finance", "instalment", "installment", "nbfc", "bajaj",
      "monthly", "month"), ("pricing", "finance", "emi", "product")),
    (("spec", "specs", "specification", "features", "camera", "battery",
      "display", "processor"), ("product",)),
    (("warranty", "replacement", "return", "gst", "invoice", "protect",
      "protection", "insurance", "damage"), ("policy", "pricing")),
)

# Review intent is matched as whole words, after the appliance energy rating
# is taken out: "5 star rating inverter ac" is a BEE label, not a review
# question, and a substring "rating" would also have caught "operating".
_ENERGY_STARS = re.compile(
    r"\b\d(?:\.\d)?\s*-?\s*stars?\b(?:\s+(?:energy|bee|power))?(?:\s+ratings?)?"
    r"|\b(?:energy|bee|power)\s+(?:star\s+)?ratings?\b|\bstar\s+ratings?\b")
_REVIEW_ASK = re.compile(r"\b(?:reviews?|ratings?|rated|stars|feedback)\b")

COMPARE_MAX = 4
TOTAL_BUDGET_MS = 9000

_WORD = re.compile(r"[a-z0-9]+")
_NOISE = {"the", "and", "for", "with", "best", "price", "of", "in", "a", "an",
          "buy", "online", "new", "me", "show", "is", "it"}


def plan(sections=None, question: str = "") -> tuple[str, ...]:
    """Which sections to fetch. Explicit beats inferred; inferred beats all."""
    if sections:
        wanted = tuple(s for s in SECTIONS + OPT_IN if s in set(sections))
        return wanted or SECTIONS
    text = (question or "").lower()
    found: list[str] = []
    for words, gives in INTENTS:
        if any(w in text for w in words):
            found += [s for s in gives if s not in found]
    if _REVIEW_ASK.search(_ENERGY_STARS.sub(" ", text)):
        found.append("reviews")
    return tuple(s for s in SECTIONS + OPT_IN if s in found) or SECTIONS


def choose(query: str, hits: list, limit: int = 5) -> tuple[str | None, list]:
    """(item_code, candidates) — a variant question has one answer.

    "iphone 15 128gb black" names one product and should be answered. "iphone
    15" names six, and picking one of them silently is how a shopper is shown
    the price of a phone they did not ask about. So: answer when one hit
    matches more of what was typed than any other, otherwise hand back the
    shortlist and let one short question settle it.
    """
    documents = [h.get("document") or {} for h in (hits or [])]
    documents = [d for d in documents if d.get("item_code")]
    if not documents:
        return None, []

    # A digit on its own is a product code, not noise — `choose("3", ...)`
    # has to find item 3. Everything else needs two characters to count.
    asked = {w for w in _WORD.findall((query or "").lower())
             if (len(w) > 1 or w.isdigit()) and w not in _NOISE}

    # An item code typed straight in is not a guess.
    for doc in documents:
        if str(doc.get("item_code")) in asked:
            return str(doc["item_code"]), []

    def score(doc) -> int:
        name = (doc.get("name") or "").lower()
        return sum(1 for w in asked if w in name)

    ranked = sorted(documents, key=score, reverse=True)
    if len(ranked) == 1 or (asked and score(ranked[0]) > score(ranked[1])):
        return str(ranked[0]["item_code"]), []

    return None, [{"item_code": str(d["item_code"]), "name": d.get("name"),
                   "price": d.get("special_price"), "mrp": d.get("price"),
                   "variant": d.get("ram") or d.get("storage")}
                  for d in ranked[:limit]]


def _url(value) -> str:
    """An item code or SKU as one URL component. A code is a caller's input;
    "%" or "../x" must reach PIM as a code that is not found, not as a path."""
    return quote(str(value), safe="")


# What an item code looks like. The shop's are digits; this leaves room for
# the SKU-style codes of other catalogues and for nothing that could steer a
# URL or a query string.
_ITEM_CODE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def item_codes_of(value, limit: int) -> tuple[list, str | None]:
    """(codes, None) from a request's `item_codes`, or ([], why) for a 400.

    At most `limit` of them, each a plain item code, duplicates dropped. The
    length is checked before anything is done with the list, so a body of a
    hundred thousand codes costs one comparison, not a stalled event loop.
    """
    if not isinstance(value, list):
        return [], 'item_codes must be a list, e.g. ["55828", "55565"]'
    if not value:
        return [], 'POST {"item_codes": ["55828", "55565"]}'
    if len(value) > limit:
        return [], f"ask about up to {limit} items per call"
    codes = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            return [], "each item code must be a string or a number"
        code = str(raw).strip()
        if not _ITEM_CODE.fullmatch(code):
            return [], f"not an item code: {code[:40]!r}"
        if code not in codes:
            codes.append(code)
    return codes, None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def _section(status: str, source: str, took_ms: int, data: dict,
             extra: dict | None = None) -> dict:
    out = {"status": status, "source": source, "fetched_at": _now_iso(),
           "took_ms": took_ms}
    out.update(extra or {})
    if status in (apis.OK, apis.EMPTY):
        out["data"] = data
    elif status == apis.NOT_CONFIGURED:
        # The upstream `detail` names internal gateway hosts, env-var names and
        # whose tokens are in play (see apis.MISSING_NOTE) — fine for a server
        # log, not for the API body. Return a generic reason to callers.
        out["reason"] = f"{source} is not configured in this environment"
    else:
        out["reason"] = (data or {}).get("reason") or (data or {}).get("detail")
    return out


def _adapt(status: str, got: dict) -> str:
    """A 404 we could not read means the item is not there.

    PIM answers a code it does not hold with a 404 and a body that has no
    `data.product` in it. That is not a malformed response, which is what
    the adapter can only call it on its own — it is an absent product, and
    the two deserve different words.
    """
    if status == apis.INVALID and got.get("code") == 404:
        return apis.EMPTY
    return status


def _carry(got: dict) -> dict:
    """What a failed call is allowed to say about itself."""
    extra = {}
    for flag in ("cached", "joined", "retried"):
        if got.get(flag):
            extra[flag] = True
    if got.get("code") is not None:
        extra["http"] = got["code"]
    return extra


async def _product(env, item, ctx) -> dict:
    got = await apis.fetch(env, "pim", f"/product/{_url(item)}", kind="pim_product",
                           **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "pim-product", got.get("took_ms", 0),
                        got, _carry(got))
    status, data = adapters.product(got["body"])
    return _section(_adapt(status, got), "pim-product", got["took_ms"], data,
                    _carry(got))


async def _pricing(env, item, ctx, channel) -> dict:
    got = await apis.fetch(env, "pim", f"/pricing/{_url(item)}", kind="pim_pricing",
                           **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "pim-pricing", got.get("took_ms", 0),
                        got, _carry(got))
    status, data = adapters.pricing(got["body"], channel)
    return _section(_adapt(status, got), "pim-pricing", got["took_ms"], data,
                    _carry(got))


async def _stock(env, item, ctx) -> dict:
    got = await apis.fetch(env, "ci", "/deliverytype/stock",
                           query=f"?item_code={_url(item)}", kind="ci_stock", **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "central-inventory",
                        got.get("took_ms", 0), got, _carry(got))
    status, data = adapters.stock(got["body"])
    # A gateway that answered but refused us is not an empty shelf. This is
    # the one section where getting the distinction wrong is visible to a
    # customer as a Notify Me button on a product that is in stock.
    return _section(status, "central-inventory", got["took_ms"], data,
                    _carry(got))


async def _siblings(env, item, ctx, product_block: dict) -> dict:
    """Whether each sibling variant PIM lists beside this item can be bought.

    Two facts per sibling, asked together: Central Inventory's live stock and
    PIM's own listing switch. Stock alone is not enough — Moto 55392 has
    units in a branch and a listing switched off, so a chip that trusted the
    count sent shoppers to a "Currently Unavailable" page. A sibling whose
    listing is off is `in_stock: false, listed: false`, as `one()` reports
    the page's own item.

    Anything that could not be asked is `None` — unknown — and never `False`:
    a chip marked "out of stock" on a guess is the stale-index nudge this
    replaced, which sent shoppers to a MacBook with no units.
    """
    product_block = product_block or {}
    if product_block.get("status") != apis.OK:
        # Nothing was asked of Central Inventory, so its name is not on this.
        return _section(apis.NOT_REQUESTED, "central-inventory", 0,
                        {"reason": "needs the product record, which lists the "
                                   "siblings, and it came back "
                                   f"{product_block.get('status') or 'missing'}"})
    options = (((product_block.get("data") or {}).get("variants") or {})
               .get("options") or [])
    codes = []
    for option in options:
        code = option.get("item_code")
        # No chip opens an inactive sibling, or one that needs a value PIM has
        # switched off, so whether it can be bought is moot.
        if (code and code != item and code not in codes
                and option.get("active") and option.get("offered", True)):
            codes.append(code)
    codes = codes[:SIBLINGS_MAX]
    if not codes:
        return _section(apis.EMPTY, "central-inventory", 0, {"stock": {}})

    stock = await _availability_of(env, codes, ctx, SIBLINGS_IN_FLIGHT)
    if not any(s["in_stock"] is not None for s in stock.values()):
        return _section(apis.UNAVAILABLE, "central-inventory", 0,
                        {"reason": "neither Central Inventory nor PIM answered "
                                   "for any sibling"})
    return _section(apis.OK, "central-inventory", 0, {"stock": stock})


async def _available(env, code, ctx) -> dict:
    """Can this one be bought right now, as its own product page would say?

    Central Inventory's live count and PIM's listing switch, asked together.
    The switch wins whatever the count says — an unlisted item reads
    "Currently Unavailable" on its own page with one unit in a branch, with
    none, or with the stock gateway down — so it is read every time; the PIM
    record is cached for ten minutes, so it is mostly free. `None` means not
    known, never "no".
    """
    entry = {"in_stock": None, "listed": None}
    stocked, record = await asyncio.gather(_stock(env, code, ctx),
                                           _product(env, code, ctx),
                                           return_exceptions=True)
    if isinstance(stocked, dict) and stocked.get("status") == apis.OK:
        data = stocked.get("data") or {}
        entry.update(in_stock=data.get("in_stock"), quantity=data.get("quantity"))
    if isinstance(record, dict) and record.get("status") == apis.OK:
        entry["listed"] = (record.get("data") or {}).get("listed")
        if entry["listed"] is False:
            entry["in_stock"] = False
    return entry


async def _availability_of(env, codes: list, ctx, in_flight: int) -> dict:
    """`_available` for several items, a few at a time, one shared deadline."""
    gate = asyncio.Semaphore(max(1, in_flight))
    deadline = ctx.get("deadline")
    # Both calls go out together, so neither may start on less than its own
    # timeout: a call begun on the dregs of the budget times out, and a later
    # request that joins it in flight inherits the timeout.
    floor = max(apis.SERVICES["ci"]["timeout_ms"], apis.SERVICES["pim"]["timeout_ms"])

    async def asked(code):
        async with gate:
            # Better unknown now than a poisoned answer for the next page.
            if deadline is not None and deadline.remaining_ms() < floor:
                return {"in_stock": None, "listed": None}
            return await _available(env, code, ctx)

    got = await asyncio.gather(*(asked(c) for c in codes), return_exceptions=True)
    return {code: (entry if isinstance(entry, dict) else {"in_stock": None, "listed": None})
            for code, entry in zip(codes, got)}


# The results grid shows what the catalogue index says, and the index is a
# copy that lags: MacBook Pro 55828 sat in the "In Stock" filter with no units
# anywhere, and its own page said Out Of Stock one click later. Central
# Inventory answers one item per call — several codes in one call come back
# as zero stock for all of them, so batching there would mark a whole page
# sold out — so a page asks here for the cards it is about to show, a screen
# at a time, rather than for a whole grid up front.
AVAILABILITY_MAX = 24
AVAILABILITY_IN_FLIGHT = 8


async def availability(env, item_codes: list, *, channel: str = "online",
                       deadline=None) -> dict:
    """Live stock and listing state for up to AVAILABILITY_MAX items.

    The routes refuse a longer list (see item_codes_of); this cuts one down
    anyway before looking at it, so no caller can make it do more.
    """
    started = time.time()
    raw = list(item_codes or [])
    codes = list(dict.fromkeys(str(c).strip() for c in raw[:AVAILABILITY_MAX * 2]
                               if str(c).strip()))
    asked = codes[:AVAILABILITY_MAX]
    dropped = (max(0, len(codes) - AVAILABILITY_MAX)
               + max(0, len(raw) - AVAILABILITY_MAX * 2))
    ctx = {"channel": channel, "locale": "en-IN",
           "deadline": deadline or apis.Deadline(TOTAL_BUDGET_MS)}
    stock = await _availability_of(env, asked, ctx, AVAILABILITY_IN_FLIGHT)
    out = {"stock": stock, "took_ms": int((time.time() - started) * 1000)}
    if dropped > 0:
        out["not_asked"] = dropped
    return out


# How a compare table words it, in the index's own capitals.
_AVAILABILITY_WORD = {"unlisted": "Currently Unavailable", True: "In Stock",
                      False: "Out Of Stock"}


async def with_live_availability(env, compared: dict, *, channel: str) -> dict:
    """A compare table's Availability row, as each product's own page says it.

    The row is built from the index's copy of stock, and a table that says
    "In Stock" beside a card that says "Out of stock" is the contradiction
    the cards were fixed for. Compare is four products at most, well inside
    one `availability` call. Anything not answered keeps the index's word.
    """
    ids = [str(p.get("id")) for p in (compared.get("products") or [])]
    row = next((r for r in (compared.get("rows") or [])
                if r.get("label") == "Availability"), None)
    if not ids or row is None:
        return compared
    try:
        live = (await availability(env, ids, channel=channel)).get("stock") or {}
    except Exception:                                          # noqa: BLE001
        return compared
    values = list(row.get("values") or [])
    for i, code in enumerate(ids):
        v = live.get(code) or {}
        key = "unlisted" if v.get("listed") is False else v.get("in_stock")
        if key in _AVAILABILITY_WORD and i < len(values):
            values[i] = _AVAILABILITY_WORD[key]
    row["values"] = values
    row["differs"] = len({str(x) for x in values}) > 1
    return compared


async def _reviews(env, item, ctx, product_block: dict) -> dict:
    """The model's ratings and latest reviews, from the shop's review service.

    Filed under the model code the product record names, so that record is
    read first; without it there is nothing to ask for.
    """
    data = (product_block or {}).get("data") or {}
    model = data.get("model_code")
    if (product_block or {}).get("status") != apis.OK or not model:
        return _section(apis.NOT_REQUESTED, "retailer-reviews", 0,
                        {"reason": "needs the product record's model code, "
                                   "which is what reviews are filed under"})
    slug = data.get("slug") or ""
    details, listing = await asyncio.gather(
        apis.fetch(env, "reviews", "/review/getReviewDetails",
                   query=(f"?slug={_url(slug)}&productCode={_url(model)}"
                          f"&itemCode={_url(item)}"), kind="reviews", **ctx),
        apis.fetch(env, "reviews", "/review",
                   query=(f"?limit=5&page=1&productCode={_url(model)}"
                          "&sort=DESC&filter="), kind="reviews", **ctx),
        return_exceptions=True)
    pair = []
    for got in (details, listing):
        pair.append(got if isinstance(got, dict)
                    else {"status": apis.UNAVAILABLE, "took_ms": 0,
                          "detail": f"{type(got).__name__}: {str(got)[:120]}"})
    details, listing = pair
    took = max(details.get("took_ms", 0), listing.get("took_ms", 0))
    if details["status"] != apis.OK:
        return _section(details["status"], "retailer-reviews", took, details,
                        _carry(details))
    # The list failing costs the list, not the averages above it.
    shelf = (listing.get("body") or {}) if listing["status"] == apis.OK else {}
    status, data = adapters.reviews(details.get("body") or {}, shelf)
    data["model_code"] = model
    return _section(status, "retailer-reviews", took, data, _carry(details))


def pincode_of(value) -> str:
    """A six-digit Indian PIN that does not start with 0, else the default."""
    value = str(value or "").strip()
    return value if _PINCODE.fullmatch(value) else DEFAULT_PINCODE


async def _delivery(env, item, ctx, pincode: str) -> dict:
    got = await apis.fetch(env, "ci", "/deliverytype/pincode",
                           query=f"?pincode={pincode}&item_code={_url(item)}",
                           kind="ci_delivery", **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "central-inventory",
                        got.get("took_ms", 0), got, _carry(got))
    status, data = adapters.delivery(got["body"], pincode)
    status = _adapt(status, got)
    if status == apis.EMPTY:
        # A 404 the adapter could not read is "nothing to deliver" too; say it
        # in the same shape as an answered empty quote, so a page can read it.
        data = {"pincode": pincode, "options": (data or {}).get("options") or []}
    return _section(status, "central-inventory", got["took_ms"], data,
                    _carry(got))


async def _shelf(env, item, ctx, channel, path: str, adapt) -> dict:
    """A PIM product list — bought together, related — read with `adapt`."""
    # Priced lists, so cached like a price rather than like a spec sheet.
    got = await apis.fetch(env, "pim", f"/product/{_url(item)}/{path}",
                           kind="pim_lists", **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "pim-" + path, got.get("took_ms", 0),
                        got, _carry(got))
    status, data = adapt(got["body"], channel)
    return _section(_adapt(status, got), "pim-" + path, got["took_ms"], data,
                    _carry(got))


async def _offers(env, item, ctx, channel, shopcode: str) -> dict:
    detail, labels = await asyncio.gather(
        apis.fetch(env, "offers", f"/ruleEngine/offers/{_url(item)}",
                   kind="offers_detail", **ctx),
        apis.fetch(env, "offers", f"/ruleEngine/getOffers/{_url(item)}",
                   query=f"?shopcode={shopcode}", kind="offers_labels", **ctx),
        return_exceptions=True)
    pair = []
    for got in (detail, labels):
        pair.append(got if isinstance(got, dict)
                    else {"status": apis.UNAVAILABLE, "took_ms": 0,
                          "detail": f"{type(got).__name__}: {str(got)[:120]}"})
    detail, labels = pair
    took = max(detail.get("took_ms", 0), labels.get("took_ms", 0))

    if detail["status"] != apis.OK and labels["status"] != apis.OK:
        worse = detail if detail["status"] != apis.NOT_CONFIGURED else labels
        return _section(worse["status"], "offers-gateway", took, worse,
                        _carry(worse))

    status, data = adapters.offers(detail.get("body") or {},
                                   labels.get("body") or {}, channel)
    data["endpoints"] = {"detail": detail["status"], "labels": labels["status"]}
    return _section(status, "offers-gateway", took, data, _carry(detail))


# The shop's product page prints two effective prices, not one:
#
#     ₹36,999  Effective Price @ Online
#     ₹35,099  Effective Price @ Store
#
# Different offers, and sometimes a different ticket — the SHOP channel
# carried eight card offers against ONLINE's one on the product this was first
# measured from. So the second figure asks for both again under the other
# channel: the offers and the price. PIM answers with the price list of the
# channel it is asked under, and the store's can differ — Bosch fridge 55453 is
# ₹1,14,990 on SHOP against ₹1,08,990 ONLINE, and the shop's store figure
# (₹1,07,490) comes off the SHOP price, not the online one.
OTHER = {"online": "shop", "shop": "online"}


async def _finance(env, item, ctx, channel) -> dict:
    got = await apis.fetch(env, "offers", f"/nbfc/showFinance/{_url(item)}",
                           kind="nbfc_finance", **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "offers-nbfc", got.get("took_ms", 0),
                        got, _carry(got))
    status, data = adapters.finance(got["body"], channel)
    return _section(_adapt(status, got), "offers-nbfc", got["took_ms"], data,
                    _carry(got))


async def _emi(env, item, ctx, sku: str, price) -> dict:
    """Bank EMI plans. The only call in the service that has to wait.

    The calculator is keyed by the SKU and the price in paise, and both
    come out of calls we are already making — PIM product for the SKU, and
    the price block for the amount. It is the SELLING price, after any
    activation subsidy, not the list price: on the MacBook that is 588,900
    rather than 619,900, and quoting the list price put the headline EMI
    at ₹17,568 against the shop's ₹16,690. So this runs in a second wave,
    after the price block is settled. Everything else still went at once.
    """
    if not sku or not price:
        # The reason belongs in `data`, which is where `_section` reads it
        # from for a non-answer — putting it in `extra` let it be
        # overwritten with None and the section said nothing at all.
        return _section(apis.NOT_REQUESTED, "emi-calculator", 0,
                        {"reason": "needs the SKU and the selling price, and "
                                   "one of them did not arrive"})
    query = (f"?payment_gateway=pinepg&product_code={_url(sku)}"
             f"&product_amount={int(round(float(price) * 100))}"
             f"&itemcode={_url(item)}")
    got = await apis.fetch(env, "emi", "/emicalc/emi", query=query,
                           kind="emi_plans", **ctx)
    if got["status"] != apis.OK:
        return _section(got["status"], "emi-calculator", got.get("took_ms", 0),
                        got, _carry(got))
    status, data = adapters.emi(got["body"])
    return _section(status, "emi-calculator", got["took_ms"], data, _carry(got))


async def _policy(env, item, ctx, price, mrp=None) -> dict:
    """What comes with the product, and what can be added to it.

    Two PIM calls, fetched together the way the two offer endpoints are.
    `addons` answers 404 with `{"error": {"message": "no data found"}}` for
    a product with no plan — which is an empty shelf, not a failure — so a
    missing addons body still lets the assurance strip through.
    """
    policy_got, addons_got = await asyncio.gather(
        apis.fetch(env, "pim", f"/product/{_url(item)}/policy", kind="pim_product",
                   **ctx),
        apis.fetch(env, "pim", f"/product/{_url(item)}/addons", kind="pim_product",
                   **ctx),
        return_exceptions=True)
    pair = []
    for got in (policy_got, addons_got):
        pair.append(got if isinstance(got, dict)
                    else {"status": apis.UNAVAILABLE, "took_ms": 0,
                          "detail": f"{type(got).__name__}: {str(got)[:120]}"})
    policy_got, addons_got = pair
    took = max(policy_got.get("took_ms", 0), addons_got.get("took_ms", 0))

    if policy_got["status"] != apis.OK and addons_got["status"] != apis.OK:
        return _section(policy_got["status"], "pim-policy", took, policy_got,
                        _carry(policy_got))
    status, data = adapters.policy(policy_got.get("body") or {},
                                   addons_got.get("body") or {}, price, mrp)
    data["endpoints"] = {"policy": policy_got["status"],
                         "addons": addons_got["status"]}
    return _section(status, "pim-policy", took, data, _carry(policy_got))


async def one(env, item_code: str, *, channel: str = "online",
              sections: tuple = SECTIONS, deadline=None, locale: str = "en-IN",
              shopcode: str = "", also_channel: bool = False,
              emi_hint: dict | None = None, pincode: str = "") -> dict:
    """Everything asked for about one item, fetched at once."""
    started = time.time()
    item = str(item_code).strip()
    deadline = deadline or apis.Deadline(TOTAL_BUDGET_MS)
    ctx = {"channel": channel, "deadline": deadline, "locale": locale}
    pincode = pincode_of(pincode)

    jobs = {
        "product": lambda: _product(env, item, ctx),
        "pricing": lambda: _pricing(env, item, ctx, channel),
        "stock": lambda: _stock(env, item, ctx),
        "offers": lambda: _offers(env, item, ctx, channel, shopcode),
        "finance": lambda: _finance(env, item, ctx, channel),
        "delivery": lambda: _delivery(env, item, ctx, pincode),
        "together": lambda: _shelf(env, item, ctx, channel, "bought-togather",
                                   adapters.together),
        "related": lambda: _shelf(env, item, ctx, channel, "relatedproducts",
                                  adapters.related),
    }
    asked = [s for s in SECTIONS + OPT_IN if s in sections]
    # EMI is answered in the second wave; its inputs come from two of the
    # calls in the first. Asking for it implies asking for those.
    wants_emi = "emi" in asked
    # A percentage protection plan is priced off the SELLING price, so this
    # waits for the price block too rather than quoting "20".
    wants_policy = "policy" in asked
    # The siblings are named by the product record, and the reviews are filed
    # under the model it names, so for either that is read first.
    wants_siblings = "siblings" in asked
    wants_reviews = "reviews" in asked
    # The EMI calculator needs only a SKU and a price, and the search index
    # already has both — so when the caller hands them over (the results
    # card does, for its hover popover), EMI skips the ~1.6s PIM-product and
    # pricing fetches entirely and just calls the calculator. That is the
    # difference between a 2s hover and a fast one.
    hinted_emi = bool(wants_emi and emi_hint and emi_hint.get("sku")
                      and emi_hint.get("price"))
    deferred = {"emi", "policy", "siblings", "reviews"}
    first_wave = [s for s in asked if s not in deferred]
    if (wants_emi and not hinted_emi) or wants_policy or wants_siblings or wants_reviews:
        needs = (["pricing", "offers"]
                 if (wants_emi and not hinted_emi) or wants_policy else [])
        if (wants_emi and not hinted_emi) or wants_siblings or wants_reviews:
            needs.append("product")
        for needed in needs:
            if needed not in first_wave:
                first_wave.append(needed)
        first_wave = [s for s in SECTIONS if s in first_wave]
    # A stock or price answer is only true of a listing that is switched on,
    # and only the product record says whether it is. So that is read too —
    # in parallel, and cached — though it stays out of `requested`, which is
    # what the caller asked. Not for offers or EMI alone: those are the search
    # page's hover card, which must stay one fast call.
    if ("stock" in first_wave or "pricing" in first_wave) and "product" not in first_wave:
        first_wave = [s for s in SECTIONS if s in first_wave or s == "product"]

    other = OTHER.get(channel.lower())
    other_task = other_pricing_task = None
    if also_channel and other and "offers" in first_wave:
        other_ctx = dict(ctx, channel=other)
        other_task = asyncio.ensure_future(
            _offers(env, item, other_ctx, other, shopcode))
        # Their ticket too: PIM answers with the price list of the channel it
        # is asked under, and the store's can differ from ours.
        other_pricing_task = asyncio.ensure_future(
            _pricing(env, item, other_ctx, other))

    # One gather, so the slowest service sets the wall clock rather than the
    # sum of them. Measured from a laptop, PIM product is the long pole at
    # roughly 1.5s while the others sit near 300-900ms: in series that is
    # about 4s of page, in parallel it is one PIM product call.
    results = await asyncio.gather(*(jobs[s]() for s in first_wave),
                                   return_exceptions=True)

    out: dict = {}
    for name, result in zip(first_wave, results):
        if isinstance(result, Exception):
            # A section that raised is a bug in us, not an upstream outage.
            # It still must not take the other four down with it.
            out[name] = _section(apis.INVALID, name, 0,
                                 {"reason": f"{type(result).__name__}: "
                                            f"{str(result)[:160]}"})
        else:
            out[name] = result
    # What the shop itself prints: the selling price after any activation
    # subsidy, then the effective price after the single best card offer.
    # Derived here rather than in a section, because it needs two of them
    # and belongs to neither — and because the EMI call depends on it.
    summary = {}
    pricing_block = out.get("pricing") or {}
    offers_block = out.get("offers") or {}
    if pricing_block.get("status") == apis.OK:
        summary = adapters.price_block(pricing_block.get("data") or {},
                                       (offers_block.get("data") or {})
                                       if offers_block.get("status") in
                                       (apis.OK, apis.EMPTY) else {},
                                       channel)
        if offers_block.get("status") not in (apis.OK, apis.EMPTY):
            # Without the offers we have a list price and nothing else. Say
            # that, rather than presenting it as the price the shop shows.
            summary["offers_unavailable"] = True
            summary["basis"] = ("offers could not be read, so this is the "
                                "price list figure and not the shop's")
            # Nor an effective price: without the offers it would be a guess.
            summary.pop("effective_price", None)
            summary.pop("offers_considered", None)

    # The other channel's effective price, when asked for. Its calls went out
    # with the first wave, so it costs nothing on the clock. Both are taken
    # here whether or not they are used, so neither is left pending, and a
    # failure in either is a missing store figure, never a failed page.
    other = OTHER.get(channel.lower())
    block = their_pricing = None
    if other_task is not None:
        got = await asyncio.gather(other_task, other_pricing_task,
                                   return_exceptions=True)
        block, their_pricing = (r if isinstance(r, dict) else None for r in got)
    if (also_channel and other and summary.get("selling_price") is not None
            and block and block.get("status") in (apis.OK, apis.EMPTY)):
        # Off their own ticket. PIM answering "no price list for that channel"
        # (EMPTY) means the shop prices both off this one, so ours stands in.
        # Any other answer — a timeout, an outage — means the store's ticket
        # is unknown, and borrowing ours would print a store price the shop
        # does not show (the fridge read ₹1,01,490 where the shop says
        # ₹1,07,490), so the store figure is left out instead.
        said = (their_pricing or {}).get("status")
        base = ((their_pricing or {}).get("data") if said == apis.OK
                else pricing_block.get("data") if said == apis.EMPTY else None)
        try:
            theirs = (adapters.price_block(base, block.get("data") or {}, other)
                      if base else {})
        except Exception:                                      # noqa: BLE001
            theirs = {}
        if theirs.get("effective_price") is not None:
            summary["other_channel"] = {
                "channel": other,
                "selling_price": theirs.get("selling_price"),
                "list_price": theirs.get("list_price"),
                "activation_discount": theirs.get("activation_discount"),
                "card_offers": theirs.get("card_offers"),
                "effective_price": theirs["effective_price"],
                "best_offer": theirs.get("best_offer"),
                "offers_considered": theirs.get("offers_considered"),
            }

    second = {}
    if wants_emi:
        if hinted_emi:
            sku, price = emi_hint["sku"], emi_hint["price"]
        else:
            product_data = (out.get("product") or {}).get("data") or {}
            sku, price = product_data.get("sku"), summary.get("selling_price")
        second["emi"] = _emi(env, item, ctx, sku, price)
    if wants_policy:
        second["policy"] = _policy(env, item, ctx, summary.get("selling_price"),
                                   summary.get("mrp"))
    if wants_siblings:
        second["siblings"] = _siblings(env, item, ctx, out.get("product"))
    if wants_reviews:
        second["reviews"] = _reviews(env, item, ctx, out.get("product"))
    if second:
        done = await asyncio.gather(*second.values(), return_exceptions=True)
        for name, result in zip(second, done):
            out[name] = (result if isinstance(result, dict)
                         else _section(apis.INVALID, name, 0,
                                       {"reason": f"{type(result).__name__}: "
                                                  f"{str(result)[:160]}"}))

    for name in SECTIONS:
        out.setdefault(name, {"status": apis.NOT_REQUESTED})
    stock_block = out.get("stock") or {}
    if stock_block.get("status") == apis.OK:
        summary["in_stock"] = (stock_block.get("data") or {}).get("in_stock")
    # A listing PIM has switched off is not for sale, whatever a branch still
    # counts. The summary is what the shop shows, and the shop shows
    # "Currently Unavailable": no price, no offers, nothing to buy. The figures
    # stay in the sections, where they are what each service said.
    product_block = out.get("product") or {}
    if (product_block.get("status") == apis.OK
            and (product_block.get("data") or {}).get("listed") is False):
        summary["listed"] = False
        summary["in_stock"] = False
        summary["availability"] = "Currently Unavailable"
        # Off the summary, so nothing reading it quotes a price the shop does
        # not show; kept beside it, because they are still what PIM and the
        # rule engine said. EMI stays: the shop prints that line regardless.
        held = {k: summary.pop(k) for k in UNLISTED_HIDES if k in summary}
        if held:
            summary["unlisted_figures"] = held
        prior = summary.get("basis")
        summary["basis"] = ("the listing is switched off in PIM, so the shop "
                            "shows no price and nothing to buy"
                            + (f"; {prior}" if prior else ""))
    elif "product" in out and product_block.get("status") not in (
            apis.OK, apis.NOT_REQUESTED):
        # Asked and not answered: whether this is for sale is not known.
        summary["listing_unknown"] = True
    emi_block = out.get("emi") or {}
    if emi_block.get("status") == apis.OK:
        summary["emi_starts_from"] = (emi_block.get("data") or {}).get("headline")

    answered = [n for n in asked if out[n]["status"] in (apis.OK, apis.EMPTY)]
    return {
        "item_code": item,
        "channel": channel,
        "summary": summary,
        "sections": out,
        "requested": asked,
        "answered": answered,
        "partial": [n for n in asked if n not in answered],
        "took_ms": int((time.time() - started) * 1000),
    }


async def many(env, item_codes: list, *, in_flight: int = 2, **kwargs) -> list:
    """A short compare list. Bounded, because four products is twenty calls.

    Two items at a time rather than all four: the gateways are shared with
    the shop itself, and a comparison is not worth a burst. In-flight
    deduplication in `apis` means a repeated item costs nothing.
    """
    codes, seen = [], set()
    for code in item_codes:
        code = str(code).strip()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    codes = codes[:COMPARE_MAX]

    deadline = kwargs.pop("deadline", None) or apis.Deadline(TOTAL_BUDGET_MS)
    # A comparison already shows several products side by side; pricing each
    # of them on two channels as well doubles the offer calls to say
    # something nobody asked. The second channel is a product-page feature.
    kwargs.pop("also_channel", None)
    kwargs.pop("pincode", None)
    # Asked only for what a comparison never fetches: compare on the defaults,
    # as plan() does for a list it cannot use, rather than return nothing.
    picked = tuple(s for s in (kwargs.get("sections") or SECTIONS)
                   if s not in PAGE_ONLY and s not in OPT_IN)
    kwargs["sections"] = picked or tuple(s for s in SECTIONS if s not in PAGE_ONLY)
    gate = asyncio.Semaphore(max(1, in_flight))

    async def guarded(code):
        async with gate:
            return await one(env, code, deadline=deadline, **kwargs)

    done = await asyncio.gather(*(guarded(c) for c in codes),
                                return_exceptions=True)
    return [d if isinstance(d, dict)
            else {"item_code": code, "error": f"{type(d).__name__}: {d}"}
            for code, d in zip(codes, done)]
