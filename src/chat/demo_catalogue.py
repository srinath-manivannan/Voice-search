"""Two variants of one handset, for the local widget stack.

Nothing here is a live price or a retailer feed. "Is this available?" can
be walked end to end: the 256 GB colour is out of stock and the 128 GB
colour is in stock nearby, which is the sibling the guide is meant to offer.
"""

from __future__ import annotations

from src.chat.allowlist import ModelProduct, PriceFact, Specs, StockFact

_WHEN = "2026-11-26T12:10:06+05:30"
_SKU = "SKU-DEMO-A1"
_NAME = "Demo Handset A1"


def _item(
    variant_id: str,
    label: str,
    storage_gb: int,
    price: int,
    stock_state: str,
    stores: int,
) -> ModelProduct:
    return ModelProduct(
        ref="?",
        sku_id=_SKU,
        variant_id=variant_id,
        brand="Samsung",
        model_name=_NAME,
        model_number="A1-DEMO",
        variant_label=label,
        category="phone",
        price=PriceFact(price, price + 2000, "known", _WHEN),
        stock=StockFact(
            stock_state,
            stores,
            "nearby",
            _WHEN,
        ),
        specs=Specs(
            ois=True,
            video_max="4K30",
            usb="usb_c",
            ram_gb=8,
            storage_gb=storage_gb,
            colour="Black",
            five_g=True,
        ),
    )


_ITEMS: tuple[ModelProduct, ...] = (
    _item("VAR-128-BLK", "8 GB + 128 GB, Black", 128, 18499, "in_stock", 3),
    _item("VAR-256-BLK", "8 GB + 256 GB, Black", 256, 20499, "out_of_stock", 0),
)


class DemoCatalogue:
    async def search(
        self,
        *,
        brand: str | None,
        max_price: int | None,
        category: str = "phone",
        min_price: int | None = None,
    ) -> list[ModelProduct]:
        del max_price
        if brand and brand.casefold() != "samsung":
            return []
        return list(_ITEMS)

    async def lookup(self, product_id: str, variant_id: str) -> ModelProduct | None:
        for item in _ITEMS:
            if item.sku_id == product_id and (
                not variant_id or item.variant_id == variant_id
            ):
                return item
        return None
