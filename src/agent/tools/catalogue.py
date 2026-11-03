"""Product lookups against the existing Search Blocks Worker.

The Worker is already built, already deployed and already knows the catalogue.
This is a client for it, not a second search engine: POST / with {"q": "..."},
an X-Search-Token header, and read `hits[].document`.

WHAT THIS DELIBERATELY DOES NOT DO, and why it matters more than what it does.

Stock. src/worker/search.py requires `availability` and `stock_status` to BOTH
read "In Stock", because they disagree on 191 live products and the optimistic
reading sells things that cannot be bought. Its own comment then says that
even when both agree, 7 of 12 sampled were out of stock on the real site, and
"no filter here can see that". So this returns a stock *hint*, never a fact,
and the persona is told to offer to confirm rather than promise.

EMI, exchange offers and store locations. They are not in the catalogue - not
thin, absent. The reference agent advertises all three in its suggested
questions; we cannot answer any of them truthfully, so there is no tool here
that pretends to. The agent hands those to a person, which is what it already
does for anything it does not know.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

MAX_QUERY_CHARS = 150  # the Worker rejects longer
MAX_RESULTS_SPOKEN = 3  # nobody can hold a read-out list of more


class Poster(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...


def _default_poster() -> Poster:
    import httpx

    return httpx.AsyncClient(timeout=10.0)


@dataclass(frozen=True)
class Product:
    name: str
    price: int | None
    brand: str | None
    item_code: str | None
    stock_hint: bool

    def spoken(self) -> str:
        price = f"₹{self.price:,}" if self.price else "price not listed"
        return f"{self.name} at {price}"


@dataclass(frozen=True)
class CatalogueResult:
    products: list[Product]
    total: int
    understood: dict[str, Any]

    @property
    def found_nothing(self) -> bool:
        return not self.products


class CatalogueUnavailable(RuntimeError):
    """The Worker could not be reached or refused us.

    Raised rather than returning an empty result: "we have nothing matching"
    and "the catalogue is down" are different things to say to a customer, and
    an empty list would make the agent say the first when the second is true.
    """


class Catalogue:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/"
        self._token = token
        self._poster_factory = poster_factory or _default_poster

    @staticmethod
    def _product(document: dict[str, Any]) -> Product:
        price = document.get("price")
        return Product(
            name=str(document.get("name") or "").strip() or "Unnamed product",
            price=int(price) if isinstance(price, (int, float)) and price > 0 else None,
            brand=document.get("brand") or None,
            item_code=str(document.get("item_code"))
            if document.get("item_code")
            else None,
            # Both fields had to agree for the Worker to call this in stock,
            # and even then the feed can lag the shop. A hint, not a promise.
            stock_hint=str(document.get("stock_status") or "").lower() == "in stock",
        )

    async def search(
        self,
        query: str,
        max_price: int | None = None,
        in_stock_only: bool = False,
        limit: int = MAX_RESULTS_SPOKEN,
    ) -> CatalogueResult:
        body: dict[str, Any] = {"q": query.strip()[:MAX_QUERY_CHARS]}
        if max_price is not None:
            # The Worker reads a budget out of the query text itself, so this
            # is appended rather than sent as a field it does not accept.
            body["q"] = f"{body['q']} under {max_price}"[:MAX_QUERY_CHARS]
        if in_stock_only:
            body["in_stock"] = True

        headers = {"X-Search-Token": self._token} if self._token else {}
        client = self._poster_factory()

        try:
            response = await client.post(self._url, json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 - any transport failure is the same to the caller
            raise CatalogueUnavailable(str(exc)[:200]) from exc

        if response.status_code != 200:
            raise CatalogueUnavailable(f"search returned {response.status_code}")

        payload = response.json()
        hits = payload.get("hits") or []
        return CatalogueResult(
            products=[self._product(h.get("document") or {}) for h in hits[:limit]],
            total=int(payload.get("total") or payload.get("found") or 0),
            understood=payload.get("understood") or {},
        )
