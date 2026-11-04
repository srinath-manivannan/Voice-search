"""Product search against Poorvika's Product List API.

The API is Typesense's own multi_search shape behind a proxy, which is the
same thing the Search Blocks Worker talks to - so the query shape here is
borrowed from a system already in production rather than invented.

Three things were established by probing it, and each one shapes the code:

`cat_anc_codes` is not filterable. The schema rejects it outright, so category
filtering goes through `categories`, where "Mobiles" is the value that means
phones.

The two stock fields disagree. `availability` and `stock_status` differ on
hundreds of live products, and Search Blocks checked them against the shop:
stock_status was right on 13 of 13 sampled, availability on none. Both must
read In Stock, and `variant_status:=1` on top, or the agent offers things
nobody can buy. With all three the 20,000-30,000 band returns 8 phones; with
none it returns hundreds, most of them unbuyable.

Even then the feed can lag the shop - Search Blocks found 7 of 12 sampled
out of stock despite both fields agreeing - so nothing here promises stock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

COLLECTION = "productsnew"
SHORTLIST = 3  # nobody can hold a longer list read aloud
QUERY_BY = "name,categories,item_code,sku,ram"
QUERY_WEIGHTS = "4,3,2,1,1"

# Spoken category words to the one value the schema filters on.
CATEGORIES = {
    "phone": "Mobiles",
    "mobile": "Mobiles",
    "laptop": "Laptops",
    "tablet": "Tabs & IPad",
    "watch": "Smart Watches",
    "tv": "Televisions",
}

# Both stock fields must agree, and the variant must be one the shop lists.
IN_STOCK = "availability:=`In Stock` && stock_status:=`In Stock` && variant_status:=1"


class Poster(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...


def _default_poster() -> Poster:
    import httpx

    return httpx.AsyncClient(timeout=10.0)


class CatalogueUnavailable(RuntimeError):
    """The search could not be reached or refused us.

    Distinct from finding nothing: "we have nothing matching" and "the
    catalogue is down" are different sentences to say to a customer.
    """


@dataclass(frozen=True)
class Product:
    item_code: str
    name: str
    price: int | None
    brand: str | None
    in_stock: bool

    def spoken(self) -> str:
        """What the agent may say. No item code - that is internal."""
        price = f"₹{self.price:,}" if self.price else "price not listed"
        return f"{self.name} - {price}"


@dataclass(frozen=True)
class SearchResult:
    products: list[Product]
    total: int

    @property
    def found_nothing(self) -> bool:
        return not self.products


class Catalogue:
    def __init__(
        self,
        url: str,
        token: str,
        origin: str = "bot",
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._origin = origin
        self._poster_factory = poster_factory or _default_poster

    @staticmethod
    def _filter(
        category: str | None,
        min_price: int | None,
        max_price: int | None,
        in_stock: bool,
    ) -> str:
        clauses = []
        if category and (value := CATEGORIES.get(category.lower().strip())):
            clauses.append(f"categories:={value}")
        if min_price is not None:
            clauses.append(f"price:>={int(min_price)}")
        if max_price is not None:
            clauses.append(f"price:<={int(max_price)}")
        if in_stock:
            clauses.append(IN_STOCK)
        return " && ".join(clauses)

    @staticmethod
    def _product(document: dict[str, Any]) -> Product:
        price = document.get("price")
        both_agree = (
            str(document.get("availability") or "").lower() == "in stock"
            and str(document.get("stock_status") or "").lower() == "in stock"
        )
        return Product(
            item_code=str(document.get("item_code") or ""),
            name=str(document.get("name") or "").strip() or "Unnamed product",
            price=int(price) if isinstance(price, (int, float)) and price > 0 else None,
            brand=document.get("brand_name") or None,
            in_stock=both_agree,
        )

    async def search(
        self,
        query: str = "*",
        category: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        in_stock_only: bool = True,
        limit: int = SHORTLIST,
    ) -> SearchResult:
        search: dict[str, Any] = {
            "collection": COLLECTION,
            "q": (query or "*").strip() or "*",
            "query_by": QUERY_BY,
            "query_by_weights": QUERY_WEIGHTS,
            # Dearest first inside a budget: a customer asking for "under
            # 30,000" is usually after the most they can get for it.
            "sort_by": "price:desc" if max_price else "_text_match:desc",
            "page": 1,
            "per_page": max(limit, 1),
            "use_cache": True,
            "cache_ttl": 120,
            "num_typos": 2,
        }
        if filter_by := self._filter(category, min_price, max_price, in_stock_only):
            search["filter_by"] = filter_by

        client = self._poster_factory()
        try:
            response = await client.post(
                self._url,
                headers={
                    "Content-Type": "application/json",
                    "Origin": self._origin,
                    "access-token": self._token,
                },
                json={"searches": [search]},
            )
        except Exception as exc:  # noqa: BLE001 - every transport failure is the same here
            raise CatalogueUnavailable(str(exc)[:200]) from exc

        if response.status_code != 200:
            raise CatalogueUnavailable(f"search returned {response.status_code}")

        result = (response.json().get("results") or [{}])[0]
        if error := result.get("error"):
            # A rejected filter is a bug in our query, not an empty shelf.
            raise CatalogueUnavailable(f"search rejected the query: {error[:160]}")

        hits = result.get("hits") or []
        return SearchResult(
            products=[self._product(h.get("document") or {}) for h in hits[:limit]],
            total=int(result.get("found") or 0),
        )
