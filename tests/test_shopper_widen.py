"""A hard budget that matches nothing must not get stuck there."""

from __future__ import annotations

from src.chat.allowlist import ModelProduct, PriceFact, Specs, StockFact
from src.chat.shopper import ShopperGuide, Slots

_WHEN = "2026-11-26T12:10:00+05:30"


def _phone(name: str, price: int, sku: str) -> ModelProduct:
    return ModelProduct(
        ref="?",
        sku_id=sku,
        variant_id="V",
        brand="Demo",
        model_name=name,
        model_number=name,
        variant_label="8 GB",
        category="phone",
        price=PriceFact(price, price + 1000, "known", _WHEN),
        stock=StockFact("in_stock", 2, "nearby", _WHEN),
        specs=Specs(ram_gb=8, storage_gb=128),
    )


class Shelf:
    def __init__(self, items: list[ModelProduct]) -> None:
        self.items = items

    async def search(
        self,
        *,
        brand: str | None,
        max_price: int | None,
        category: str = "phone",
        min_price: int | None = None,
    ) -> list:
        del max_price
        if brand and brand.casefold() != "demo":
            return []
        return list(self.items)

    async def lookup(self, product_id: str, variant_id: str):
        del product_id, variant_id
        return None


async def test_under_40k_then_please_then_under_50k_returns_products():
    guide = ShopperGuide(
        Shelf(
            [
                _phone("Handset 45", 45_000, "SKU-45"),
                _phone("Handset 62", 62_000, "SKU-62"),
            ]
        )
    )
    first = await guide.respond(Slots(), "i need under 40K")
    assert first.cards == []
    assert "that brand" not in first.text.casefold()
    assert "look a little wider" in first.text.casefold()
    assert "₹40,000" in first.text

    widened = await guide.respond(Slots.from_dict(first.slots), "please")
    assert widened.cards
    assert "that brand" not in widened.text.casefold()
    prices = [card["price"]["selling_inr"] for card in widened.cards]
    assert 45_000 in prices
    assert all(price <= 50_000 for price in prices)

    raised = await guide.respond(Slots.from_dict(widened.slots), "under 50K")
    assert raised.cards
    assert "that brand" not in raised.text.casefold()
    again = [card["price"]["selling_inr"] for card in raised.cards]
    assert 45_000 in again
    assert 62_000 not in again
    assert "nothing matching" not in raised.text.casefold()
