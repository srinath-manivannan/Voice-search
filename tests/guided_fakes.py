"""Stand-ins for the guided call's two upstreams and its one model.

Shared by the guided test modules so that all of them agree on what the
index and the estate answer. No network, no money.
"""

from __future__ import annotations

from typing import Any

from src.agent.tools import estate as estate_api
from src.agent.tools.catalogue import CatalogueUnavailable
from src.guided.shelf import Item
from src.guided.thinker import Brief, Thought, ThinkerUnavailable


def indexed(
    code: str, name: str, price: int, brand: str = "Samsung", **fields: Any
) -> dict:
    """One document as the search index holds it. Index prices are list prices."""
    return {
        "item_code": code,
        "name": name,
        "price": price,
        "special_price": price,
        "brand_name": brand,
        "model_code": fields.pop("model_code", name.lower().replace(" ", "-")),
        **fields,
    }


def on_shelf(
    code: str,
    selling: int,
    in_stock: bool = True,
    mrp: int | None = None,
    quantity: int = 5,
    branches: int = 3,
) -> dict:
    """What the estate says of one item: the price in force and live stock."""
    return {
        "item_code": code,
        "sections": {
            "pricing": {
                "status": "ok",
                "data": {"price": selling, "mrp": mrp or selling},
            },
            "stock": {
                "status": "ok",
                "data": {
                    "in_stock": in_stock,
                    "quantity": quantity if in_stock else 0,
                    "branch_count": branches if in_stock else 0,
                    # Named the way Central Inventory names them, so the
                    # dealer step has shops to send a customer to.
                    "branches": [
                        {
                            "label": f"Shop {n}",
                            "state": "Tamil Nadu",
                            "quantity": str(2 + n),
                        }
                        for n in range(1, (branches if in_stock else 0) + 1)
                    ],
                },
            },
        },
        "summary": {"selling_price": selling, "mrp": mrp or selling},
    }


class FakeIndex:
    def __init__(self, shelves: dict[str, list[dict]], down: bool = False) -> None:
        self.shelves = shelves
        self.down = down
        self.reads: list[str] = []

    async def shelf(self, category: str, limit: int = 120) -> list[dict]:
        self.reads.append(category)
        if self.down:
            raise CatalogueUnavailable("search returned 503")
        return list(self.shelves.get(category, []))


class FakeEstate:
    """One payload per item. Records which sections were asked of which item."""

    def __init__(self, by_code: dict[str, dict]) -> None:
        self.by_code = by_code
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    async def product(self, item_code: str, sections: tuple[str, ...]) -> dict:
        self.asked.append((item_code, sections))
        if item_code not in self.by_code:
            raise estate_api.EstateUnavailable(f"no answer for {item_code}")
        payload = self.by_code[item_code]
        # Only what was asked for, as the real one answers.
        return {
            **payload,
            "sections": {k: v for k, v in payload["sections"].items() if k in sections},
        }


class FakeThinker:
    """Answers with the picks it was built with, and counts the asking."""

    def __init__(
        self,
        picks: tuple[tuple[str, str], ...] = (),
        usd: float = 0.0007,
        down: bool = False,
    ) -> None:
        self.picks = picks
        self.usd = usd
        self.down = down
        self.briefs: list[Brief] = []

    async def rank(self, brief: Brief) -> Thought:
        self.briefs.append(brief)
        if self.down:
            raise ThinkerUnavailable("the model returned 503")
        return Thought(self.picks, 1200, 130, 0, self.usd, 1300)


def phone(
    code: str,
    price: int,
    brand: str = "Samsung",
    camera: float | None = None,
    battery: float | None = None,
    rating: float | None = None,
    was: int | None = None,
    branches: int = 3,
) -> Item:
    """A phone on the shelf, already confirmed, for tests that start there."""
    specs = []
    measures = {}
    if camera is not None:
        specs.append(("கேமரா", f"{int(camera)}MP+"))
        measures["camera_mp"] = camera
    if battery is not None:
        specs.append(("பேட்டரி", f"{int(battery)}mAh+"))
        measures["battery_mah"] = battery
    return Item(
        item_code=code,
        name=f"{brand} Phone {code} 5G",
        brand=brand,
        price=price,
        was=was,
        specs=tuple(specs),
        attributes={"brand": brand},
        measures=measures,
        rating=rating,
        quantity=10,
        branches=branches,
        stock_confirmed=True,
        price_confirmed=True,
    )
