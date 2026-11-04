"""What the model is allowed to call, and how it is described to it.

One tool, deliberately. The model's job is to turn "20,000-30,000 kulla oru
phone" into a budget and a category; it is not to choose between seven
endpoints. Routing by intent is ordinary code - see VOICE-COMMERCE-PLAN.md -
because a table is faster than a round trip and cannot invent an endpoint.

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
        "calling this first."
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


def tool_config() -> dict[str, Any]:
    return {"functionDeclarations": [SEARCH_PRODUCTS]}
