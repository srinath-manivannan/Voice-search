"""What the model is allowed to call, and how it is described to it.

Two tools: one finds products, one answers about a product already found.
The model's job is to turn "20,000-30,000 kulla oru phone" into a budget and a
category, and "stock irukka?" into the thing the customer wants checked; it is
not to choose between seven endpoints. Which services each question needs is
ordinary code - see `runner.WANTED` and ../VOICE-COMMERCE-PLAN.md - because a
table is faster than a round trip and cannot invent an endpoint.

The split matters for latency as much as for correctness. Every service except
discovery is keyed by an item code, so once a shortlist exists, a question
about stock or EMI is one lookup and not a search followed by one.

The descriptions matter more than they look. They are the only thing standing
between "under 30000" and a search with no budget at all. But they are also
charged on every turn of every call, like the system instruction and for the
same reason, so each one says what the model cannot work out by itself and
stops. Anything that merely repeats the standing instruction - "never answer
without calling this first" - was paid for twice a turn and is gone from here;
persona.py still says it, once.

What is left is mostly about NOT calling a tool. Each call costs a whole extra
generation - one turn to make it, another to answer - and on a measured call
five of twenty-four generations said nothing at all. Three of those were
searches for a phone the session had already found, and one asked for stock
that the search had already fetched. So the descriptions now say what the
search result already contains, and that it stays valid.

## Why `want` has one value in it

Saying it in prose was not enough. Across 22 traces the model called
product_details three times, and one of those asked for OFFERS - which the
search result already carried, and which the runner answered out of its cache
with no round trip at all. The round trip was saved and the generation was
not: the call itself is a whole turn, and a turn re-bills the entire context
window.

So the enum is now `["emi"]` and the wasted call is unavailable rather than
discouraged. The runner stays tolerant of the other values on purpose - a
model that sends one anyway gets an answer instead of an error - but it is no
longer told they exist.
"""

from __future__ import annotations

from typing import Any

SEARCH_PRODUCTS = {
    "name": "search_products",
    "description": "Search once per request. Reuse returned refs, prices, stock and offers.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Brand, model or item name across all shelves. Omit for category/budget alone.",
            },
            "category": {
                "type": "string",
                # The big shelves only. Every one of these is a thing a
                # customer names and then wants a BUDGET applied to, which is
                # what a facet filter is for. Chargers, cases, geysers and
                # the kitchen are still sold and still findable - the query
                # reaches the whole catalogue - and listing them here would
                # cost prompt tokens on every generation of every call to
                # filter shelves nobody puts a price range on.
                "enum": [
                    "phone",
                    "laptop",
                    "tablet",
                    "watch",
                    "tv",
                    "ac",
                    "fridge",
                    "washing machine",
                    "fan",
                    "headphones",
                    "speaker",
                ],
                "description": "Optional shelf. For unlisted shelves use query.",
            },
            "min_price": {
                "type": "integer",
                "description": "Rupee floor. For a range set both min_price and max_price.",
            },
            "max_price": {
                "type": "integer",
                "description": "Rupee ceiling: under, below, within, kulla.",
            },
        },
        "required": [],
    },
}


PRODUCT_DETAILS = {
    "name": "product_details",
    "description": "Look up EMI for a search result.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Shortlist ref: '1' for first, '2' for second.",
            },
            "want": {
                "type": "array",
                "items": {"type": "string", "enum": ["emi"]},
            },
        },
        "required": ["ref", "want"],
    },
}


# The call ends when the customer says it does, and only then. Described in
# terms of what they said rather than what the agent feels, because "the
# conversation seems finished" is a judgement and "they said thanks" is not.
END_CALL = {
    "name": "end_call",
    "description": (
        "Only on customer sign-off (thank you, nandri, sari podhum, vechidunga). "
        "Never on silence or an open question. Speak the closing line first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["customer_said_goodbye", "handing_to_customer_care"],
            },
        },
        "required": ["reason"],
    },
}


def tool_config() -> dict[str, Any]:
    return {"functionDeclarations": [SEARCH_PRODUCTS, PRODUCT_DETAILS, END_CALL]}
