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
"""

from __future__ import annotations

from typing import Any

SEARCH_PRODUCTS = {
    "name": "search_products",
    "description": (
        "Poorvika's live catalogue. Each result carries `ref`, price, stock "
        "and best card offer - do not call product_details for those. "
        "Results stay valid all call: never search twice for one product."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Brand or model words, e.g. 'Samsung'. Empty for a budget "
                    "and category alone."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["phone", "laptop", "tablet", "watch", "tv"],
                "description": "'phone' covers mobiles.",
            },
            "min_price": {
                "type": "integer",
                "description": "Rupees.",
            },
            "max_price": {
                "type": "integer",
                "description": ("Rupees. Set for under, below, within, kulla."),
            },
        },
        "required": [],
    },
}


PRODUCT_DETAILS = {
    "name": "product_details",
    "description": (
        "EMI for ONE product already shown by search_products - the monthly "
        "instalment, which search results do not carry. Price, stock and "
        "offers are already there; asking here for those only costs a wait."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Which product from your last search: '1' is the first "
                    "you named, '2' the second. 'the second one' means these."
                ),
            },
            "want": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["price", "stock", "offers", "emi"],
                },
                "description": "Only what they actually asked about.",
            },
        },
        "required": ["ref", "want"],
    },
}


def tool_config() -> dict[str, Any]:
    return {"functionDeclarations": [SEARCH_PRODUCTS, PRODUCT_DETAILS]}
