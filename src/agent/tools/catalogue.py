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

The documents carry 83 fields, among them the specifications a customer
chooses between: camera, battery, memory, processor, display. Returning only
name and price is what made the agent answer "nalla camera features
irundhadhala idhai sonnen" about products it had been told nothing about -
asked to recommend, with nothing to recommend on, it invented a reason. The
specifications are numeric bands rather than exact figures ("45.1MP -
59.9MP"), so they are spoken as a floor: 45MP and up is true of everything in
that band.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

COLLECTION = "productsnew"
SHORTLIST = 3  # nobody can hold a longer list read aloud
# The most one shelf is ever asked for whole. Typesense refuses a page over
# 250; the largest shelf the guided call scripts held 70 on 2026-11-23.
SHELF_LIMIT = 120
QUERY_BY = "name,categories,item_code,sku,ram"
QUERY_WEIGHTS = "4,3,2,1,1"

# Spoken category words to the one value the schema filters on.
#
# Every value here was read off a live facet query and checked to return
# something - `tv` used to map to "Televisions", which is not a value this
# catalogue has, so the one category a customer asks for by name as often as
# phones found nothing at all. The shelf is "Television", singular.
#
# These are the shelves that can be FILTERED. They are not the shelves
# Poorvika sells: the catalogue carries over a hundred facet values and the
# text query reaches all of them, which is why the tool's description tells
# the model to search by words for anything not listed here. Checked against
# the live catalogue on 2026-11-16; counts in the comments are from that day
# and will drift.
CATEGORIES = {
    "phone": "Mobiles",  # 70
    "mobile": "Mobiles",
    "laptop": "Laptops",  # 18
    "tablet": "Tabs & IPad",  # 7
    "watch": "Smart Watches",  # 10
    "tv": "Television",  # 21
    "ac": "Air Conditioners",  # 23
    "fridge": "Refrigerators",  # 18
    "washing machine": "Washing Machines",  # 14
    "fan": "Fans",  # 30
    "headphones": "Headphones & Headsets",  # 34
    "speaker": "Speakers",  # 18
    "powerbank": "Powerbanks",  # 12
    "charger": "Chargers",  # 10
    "case": "Cases & Covers",  # 27
    "geyser": "Geysers",  # 5
    "kitchen": "Kitchen Appliances",  # 53
    "home appliance": "Home Appliances",  # 98
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


# A rating is worth quoting when enough people left one. 4.5 from two reviews
# is noise presented as a recommendation.
RATINGS_NEEDED = 20


@dataclass(frozen=True)
class Product:
    item_code: str
    name: str
    price: int | None
    brand: str | None
    in_stock: bool
    specs: dict[str, str] = field(default_factory=dict)
    rating: float | None = None

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
        key_header: str = "access-token",
    ) -> None:
        self._url = url
        self._token = token
        self._origin = origin
        # The staging proxy reads the key from "access-token"; production
        # Typesense behind proxy.poorvika.com reads "X-TYPESENSE-API-KEY".
        self._key_header = key_header
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
    def _specs(document: dict[str, Any]) -> dict[str, str]:
        """The handful a customer actually chooses between.

        Five, not eighty-three. A voice agent that can read out a spec sheet
        will, and nobody listening to a phone call wants one - the job here is
        to give it one true thing to say about each product.
        """
        specs: dict[str, str] = {}
        if camera := _floor(document.get("primary_camera"), "MP"):
            specs["camera"] = camera
        if battery := _floor(document.get("battery_capacity"), "mAh"):
            specs["battery"] = battery
        ram, storage = (
            _first(document.get("ram")),
            _first(document.get("internal_storage")),
        )
        if ram and storage:
            specs["memory"] = f"{ram} RAM, {storage} storage"
        if processor := _first(document.get("processor_brand")):
            specs["processor"] = processor
        if _first(document.get("cellulartechnology")) == "5G":
            specs["network"] = "5G"
        return specs

    @staticmethod
    def _product(document: dict[str, Any]) -> Product:
        price = document.get("price")
        both_agree = (
            str(document.get("availability") or "").lower() == "in stock"
            and str(document.get("stock_status") or "").lower() == "in stock"
        )
        reviews = document.get("review_count") or 0
        rating = document.get("rating")
        return Product(
            item_code=str(document.get("item_code") or ""),
            name=str(document.get("name") or "").strip() or "Unnamed product",
            price=int(price) if isinstance(price, (int, float)) and price > 0 else None,
            brand=document.get("brand_name") or None,
            in_stock=both_agree,
            specs=Catalogue._specs(document),
            rating=(
                float(rating)
                if isinstance(rating, (int, float))
                and isinstance(reviews, (int, float))
                and reviews >= RATINGS_NEEDED
                else None
            ),
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

        result = (await self.multi([search]))[0]
        hits = result.get("hits") or []
        return SearchResult(
            products=[self._product(h.get("document") or {}) for h in hits[:limit]],
            total=int(result.get("found") or 0),
        )

    async def shelf(
        self, category: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Everything the index calls in stock on one shelf, as it holds it.

        Whole documents rather than `Product`s, on purpose. The guided call
        builds its buttons out of fields a voice reply never needs - tonnage,
        door type, load orientation - and the five-spec summary above exists
        precisely to stop a voice agent reading those out.

        The index's own idea of stock and price is where the guided call
        starts, not what it shows: both are re-asked of the estate before a
        customer sees either. See `src/guided/shelf.py`.
        """
        if not CATEGORIES.get(category.lower().strip()):
            # An unknown shelf would otherwise build an empty filter and
            # return the whole catalogue as if it were one shelf.
            raise ValueError(f"not a shelf that can be filtered: {category!r}")
        page_size = SHELF_LIMIT if limit is None else min(limit, SHELF_LIMIT)
        if page_size <= 0:
            return []
        query = {
            "collection": COLLECTION,
            "q": "*",
            "query_by": "name",
            "filter_by": self._filter(category, None, None, True),
            "sort_by": "review_count:desc",
            "per_page": page_size,
            "use_cache": True,
            "cache_ttl": 120,
        }
        page = 1
        documents: dict[str, dict[str, Any]] = {}
        while True:
            result = (await self.multi([{**query, "page": page}]))[0]
            hits = result.get("hits") or []
            before = len(documents)
            for hit in hits:
                doc = hit.get("document") or {}
                if code := doc.get("item_code"):
                    documents[str(code)] = doc
            if (limit is not None and len(documents) >= limit) or len(hits) < page_size:
                break
            if page * page_size >= int(result.get("found", page * page_size)):
                break
            if len(documents) == before:
                raise CatalogueUnavailable("shelf pagination stopped advancing")
            page += 1
        return list(documents.values())[:limit]

    async def multi(self, searches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Several searches in one request, answered in the order asked.

        Any one of them being rejected fails the lot: a rejected filter is a
        bug in our query, not an empty shelf, and reading it as "found
        nothing" is how a customer gets told Poorvika does not sell
        televisions.
        """
        client = self._poster_factory()
        try:
            response = await client.post(
                self._url,
                headers={
                    "Content-Type": "application/json",
                    "Origin": self._origin,
                    self._key_header: self._token,
                    # Cloudflare in front of the production proxy refuses a
                    # library's default agent (error 1010); a named one passes.
                    "User-Agent": "shopping-assistant/1.0",
                },
                json={"searches": searches},
            )
        except Exception as exc:  # noqa: BLE001 - every transport failure is the same here
            raise CatalogueUnavailable(str(exc)[:200]) from exc

        if response.status_code != 200:
            raise CatalogueUnavailable(f"search returned {response.status_code}")

        results = response.json().get("results") or []
        if len(results) != len(searches):
            raise CatalogueUnavailable(
                f"asked {len(searches)} searches, got {len(results)} answers"
            )
        for result in results:
            if error := result.get("error"):
                raise CatalogueUnavailable(f"search rejected the query: {error[:160]}")
        return list(results)


def _first(value: Any) -> str | None:
    """These fields are single-element lists more often than they are strings."""
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value or "").strip()
    return text or None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _floor(value: Any, unit: str) -> str | None:
    """ "45.1MP - 59.9MP" -> "45MP+". The band's floor is true of all of it.

    Quoting the midpoint, or either end as if it were exact, states a figure
    the index does not have. The floor with a plus is the strongest claim the
    data supports.
    """
    text = _first(value)
    if not text:
        return None
    numbers = _NUMBER.findall(text)
    if not numbers:
        return None
    return f"{int(float(numbers[0]))}{unit}+"
