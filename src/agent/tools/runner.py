"""Executing a tool call. One implementation, both surfaces.

Voice and chat ask for tools through completely different protocols - the
Live API's toolCall/toolResponse over a socket, and functionCall parts in an
HTTP body - but what a tool *does* must not differ between them. Two copies
would drift, and the drift would show up as the voice agent and the chat
agent giving different answers about the same phone.

Failures are reported as failures. "Nothing matched" and "the lookup did not
happen" are different things to tell a customer, and collapsing them into an
empty list makes the agent say the first when the second is true.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agent.tools.catalogue import Catalogue, CatalogueUnavailable

log = logging.getLogger(__name__)


async def run_tool(
    catalogue: Catalogue | None, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    if name != "search_products":
        log.warning("model asked for an unknown tool: %s", name)
        return {"error": "unknown_tool"}
    if catalogue is None:
        return {"error": "catalogue_not_configured"}

    try:
        found = await catalogue.search(
            query=str(args.get("query") or "*"),
            category=args.get("category"),
            min_price=args.get("min_price"),
            max_price=args.get("max_price"),
        )
    except CatalogueUnavailable as down:
        log.warning("catalogue unavailable: %s", down)
        return {"error": "catalogue_unavailable"}

    return {
        "total_matching": found.total,
        "products": [
            {
                "name": p.name,
                "price_inr": p.price,
                "brand": p.brand,
                # Named a hint, not a promise: the feed can lag the shop even
                # when both of its stock fields agree.
                "in_stock_hint": p.in_stock,
            }
            for p in found.products
        ],
    }
