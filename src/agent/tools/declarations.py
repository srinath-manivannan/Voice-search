"""What the model is allowed to call, and how it is described to it.

Two tools: one finds products, one answers about a product already found.
The model's job is to turn "20,000-30,000 kulla oru phone" into a budget and a
category, and "stock irukka?" into the thing the customer wants checked; it is
not to choose between seven endpoints. Which services each question needs is
ordinary code - see `runner.WANTED` and VOICE-COMMERCE-PLAN.md - because a
table is faster than a round trip and cannot invent an endpoint.

The split matters for latency as much as for correctness. Every service except
discovery is keyed by an item code, so once a shortlist exists, a question
about stock or EMI is one lookup and not a search followed by one.

The descriptions matter more than they look. They are the only thing standing
between "under 30000" and a search with no budget at all.
"""

from __future__ import annotations

from typing import Any

SEARCH_PRODUCTS = {
    "name": "search_products",
    "description": (
        "Search Poorvika's live catalogue for products the customer can buy. "
        "Call this whenever the customer asks what is available, asks for a "
        "recommendation, mentions a budget, or names a product. Returns real "
        "products with real prices. Never answer a product question without "
        "calling this first. Each result carries a `ref` - use it with "
        "product_details for anything the customer then asks about that "
        "product. Do not search again for the same product."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Brand or model words only, for example 'Samsung' or "
                    "'iPhone 15'. Leave empty when the customer gave only a "
                    "budget and a category."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["phone", "laptop", "tablet", "watch", "tv"],
                "description": "The kind of product. 'phone' covers mobiles.",
            },
            "min_price": {
                "type": "integer",
                "description": "Lowest price in rupees, when the customer gave a range.",
            },
            "max_price": {
                "type": "integer",
                "description": (
                    "Highest price in rupees. Set this whenever the customer "
                    "says under, below, within, kulla, or gives a range."
                ),
            },
        },
        "required": [],
    },
}


PRODUCT_DETAILS = {
    "name": "product_details",
    "description": (
        "Stock, EMI, bank offers and the exact price for ONE product the "
        "customer has already been shown by search_products. Call this - not "
        "search_products - whenever the customer asks whether something is "
        "available or in stock, what the EMI or monthly instalment is, what "
        "offers or card discounts apply, or what one of those products costs. "
        "Say nothing about stock, EMI or offers without calling this first: "
        "those numbers are not in the search results and must never be "
        "guessed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Which product from your last search, by its `ref`: '1' "
                    "for the first one you told the customer about, '2' for "
                    "the second, '3' for the third. If the customer says "
                    "'the second one' or 'that Samsung', that is which one "
                    "they mean."
                ),
            },
            "want": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["price", "stock", "offers", "emi"],
                },
                "description": (
                    "Only what the customer actually asked about. Asking for "
                    "all four when they asked about EMI makes them wait for "
                    "three answers nobody wanted."
                ),
            },
        },
        "required": ["ref", "want"],
    },
}


def tool_config() -> dict[str, Any]:
    return {"functionDeclarations": [SEARCH_PRODUCTS, PRODUCT_DETAILS]}
