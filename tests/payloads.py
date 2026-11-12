"""Real upstream responses, trimmed. Shared by every test that needs one.

Captured 2026-11-04 from item 53952 (Vivo Y51 Pro 5G) against production and
item 45902 against both environments. Not invented, because the bugs worth
testing for are all in the shapes - an EMI-availability row that looks like a
discount, a price the shop hides, a section that answered with nothing.
"""

from __future__ import annotations

ANSWERED = {
    "item_code": "53952",
    "channel": "online",
    "partial": [],
    "took_ms": 970,
    "summary": {
        "mrp": 34999,
        "list_price": 34999,
        "selling_price": 34999,
        "discount_percent": None,
        "inclusive_of_taxes": True,
        "effective_price": 32999,
        "best_offer": {
            "offer_id": "431151",
            "amount": 2000,
            "label": "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards",
            "payment": {
                "card_types": ["debit", "credit"],
                "transaction_types": ["emi"],
                "gateways": ["PayU"],
            },
        },
        "offers_considered": 1,
        "basis": "the single best card offer on this channel, not a total",
        "emi_starts_from": {
            "per_month": 992,
            "months": 48,
            "provider": "HDFC Bank Debit Card",
            "type": "Standard EMI",
        },
    },
    "sections": {
        "product": {
            "status": "ok",
            "data": {"name": "Vivo Y51 Pro 5G ( Crimson Red, 8GB-128GB )"},
        },
        "pricing": {
            "status": "ok",
            "data": {
                "price": 34999,
                "mrp": 34999,
                "discount_percent": None,
                "price_list": "ONLINE",
                "hide_selling_price": False,
            },
        },
        "offers": {
            "status": "ok",
            "data": {
                "offers": [
                    {
                        "offer_id": "431151",
                        "kind": "instant_discount",
                        "label": (
                            "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards"
                        ),
                        "amount": 2000,
                        "reduces_price": False,
                        "reduces_effective": True,
                        "payment": {
                            "card_types": ["debit", "credit"],
                            "transaction_types": ["emi"],
                        },
                    },
                    {
                        "offer_id": "954755",
                        "kind": "emi_availability",
                        "label": "EMI :",
                        "amount": None,
                        "reduces_price": False,
                        "reduces_effective": False,
                        "discount": {"basis": "percentage", "value": 0},
                    },
                ]
            },
        },
        "emi": {
            "status": "ok",
            "data": {
                "headline": {
                    "per_month": 992,
                    "months": 48,
                    "provider": "HDFC Bank Debit Card",
                    "type": "Standard EMI",
                },
                "kind_count": 1,
                "provider_count": 20,
            },
        },
        # Production has no working Central Inventory credential. This is the
        # real text the estate reports for it.
        "stock": {
            "status": "not_configured",
            "reason": (
                "no ci host for production: ci-gateway.poorvika.com answers "
                "but rejects both published tokens, which are staging's"
            ),
        },
    },
}
