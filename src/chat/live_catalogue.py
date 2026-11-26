"""The widget's catalogue. Search the index in bulk, enrich a selected item on demand."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.agent.tools import estate
from src.agent.tools.catalogue import (
    CATEGORIES,
    QUERY_BY,
    QUERY_WEIGHTS,
    Catalogue,
    CatalogueUnavailable,
    FilterRejected,
    list_price,
    selling_price,
)
from src.chat.allowlist import ModelProduct, PriceFact, StockFact, product_from_raw

log = logging.getLogger(__name__)
PAGE_SIZE = 40
MAX_PAGES = 20


def _values(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, list) else [raw]
    return [
        str(v).strip() for v in parts if isinstance(v, (str, int)) and str(v).strip()
    ]


def _category_matches(doc: dict[str, Any], category: str) -> bool:
    labels = {v.casefold() for v in _values(doc.get("categories"))}
    slugs = {v.casefold() for v in _values(doc.get("categories_slug"))}
    if category == "phone":
        if "smartphones" in slugs or labels.intersection(
            {"smart phones", "smartphones"}
        ):
            return True
        return not slugs and labels == {"mobiles"}
    return CATEGORIES[category].casefold() in labels


def _capacity_gb(value: Any) -> int | None:
    for label in _values(value):
        match = re.fullmatch(r"(\d+)\s*(GB|TB)?", label, re.IGNORECASE)
        if match:
            return int(match[1]) * (1024 if (match[2] or "").lower() == "tb" else 1)
    return None


def _raw(doc: dict[str, Any], category: str, now: str) -> dict[str, Any]:
    return {
        "item_code": doc.get("item_code") or doc.get("erp_item_code"),
        "variant_id": doc.get("variant_id") or "",
        "name": doc.get("name"),
        "model_number": str(doc.get("name") or "").partition(" (")[0].strip(),
        "brand": doc.get("brand_name"),
        "category": category,
        "price": {
            "selling_inr": selling_price(doc),
            "list_inr": list_price(doc),
            "fetched_at": now,
        },
        "specs": {
            "ram": _capacity_gb(doc.get("ram")),
            "storage": _capacity_gb(doc.get("internal_storage")),
            "ois": doc.get("ois"),
            "usb": doc.get("usb_type"),
            "five_g": "5G" in _values(doc.get("cellulartechnology")) or None,
        },
    }


class LiveCatalogue:
    def __init__(
        self,
        index: Catalogue,
        backend: estate.Backend | None,
        denylist: tuple[str, ...],
    ) -> None:
        self.index = index
        self.backend = backend
        self.denylist = denylist

    async def _page(self, query: str, filters: str, page: int) -> dict[str, Any]:
        search: dict[str, Any] = {
            "collection": self.index.collection,
            "q": query,
            "query_by": QUERY_BY,
            "query_by_weights": QUERY_WEIGHTS,
            "sort_by": "_text_match:desc",
            "num_typos": 3,
            "page": page,
            "per_page": PAGE_SIZE,
            "use_cache": True,
            "cache_ttl": 120,
        }
        if filters:
            search["filter_by"] = filters
        return (await self.index.multi([search]))[0]

    async def search(
        self,
        *,
        brand: str | None,
        max_price: int | None,
        category: str = "phone",
        min_price: int | None = None,
    ) -> list[ModelProduct]:
        shelf = (
            "categories_slug:=smartphones"
            if category == "phone"
            else f"categories:={CATEGORIES[category]}"
        )
        base = f"isActive:=true && stock_status:=In Stock && {shelf}"

        def price_filter(field: str) -> str:
            clauses = [base, f"{field}:>0"]
            if max_price is not None:
                clauses.append(f"{field}:<={max_price}")
            if min_price is not None:
                clauses.append(f"{field}:>={min_price}")
            return " && ".join(clauses)

        query = brand or "*"
        plans = [
            (query, price_filter(f))
            for f in ("special_price", "selling_price", "price")
            if max_price is not None or min_price is not None
        ]
        plans.extend([(query, base), (query, shelf)])
        # A keyword fallback still has to pass the same category and stock checks.
        words = (
            ["mobile", "phone", "smartphones"] if category == "phone" else [category]
        )
        plans.extend((brand or word, "") for word in words)
        for keyword, filters in dict.fromkeys(plans):
            try:
                products = await self._collect(
                    keyword, filters, category, brand, min_price, max_price
                )
            except FilterRejected:
                if not filters:
                    raise
                continue
            if products:
                return products
            log.info(
                "Catalogue returned no eligible products for %s (filtered=%s)",
                category,
                bool(filters),
            )
        return []

    async def _collect(
        self,
        query: str,
        filters: str,
        category: str,
        brand: str | None,
        low: int | None,
        high: int | None,
    ) -> list[ModelProduct]:
        rows: dict[str, ModelProduct] = {}
        now = datetime.now(timezone.utc).isoformat()
        for page in range(1, MAX_PAGES + 1):
            result = await self._page(query, filters, page)
            hits = result.get("hits")
            if not isinstance(hits, list):
                raise CatalogueUnavailable("search returned an invalid page")
            for hit in hits:
                doc = hit.get("document") if isinstance(hit, dict) else None
                if not isinstance(doc, dict) or not _category_matches(doc, category):
                    continue
                price = selling_price(doc)
                if (
                    price is None
                    or (low is not None and price < low)
                    or (high is not None and price > high)
                ):
                    continue
                if (
                    str(doc.get("isActive", "")).casefold() not in {"true", "1"}
                    or str(doc.get("stock_status")).casefold() != "in stock"
                ):
                    continue
                if str(doc.get("name", "")).casefold().lstrip().startswith("demo "):
                    continue
                item = product_from_raw(
                    _raw(doc, category, now), self.denylist, ref="?"
                )
                if (
                    item
                    and item.sku_id
                    and (not brand or item.brand.casefold() == brand.casefold())
                ):
                    rows[item.sku_id] = replace(
                        item,
                        price=replace(item.price, source="catalogue"),
                        stock=StockFact(
                            "in_stock", None, "catalogue", now, "catalogue"
                        ),
                    )
            if (
                len(rows) >= PAGE_SIZE
                or len(hits) < PAGE_SIZE
                or page * PAGE_SIZE >= int(result["found"])
            ):
                break
        else:
            if not rows:
                raise CatalogueUnavailable(
                    "catalogue search could not establish an empty result"
                )
        # Surface different models before other colours or capacities of the same one.
        families: dict[str, ModelProduct] = {}
        variants = []
        for entry in rows.values():
            model = entry.model_name.partition(" (")[0].casefold()
            if model in families:
                variants.append(entry)
            else:
                families[model] = entry
        return [*families.values(), *variants]

    async def lookup(self, product_id: str, variant_id: str) -> ModelProduct | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", product_id):
            return None
        page = await self._page(product_id, "", 1)
        for hit in page.get("hits", []):
            doc = hit.get("document", {})
            if str(doc.get("item_code") or doc.get("erp_item_code")) != product_id:
                continue
            if variant_id and str(doc.get("variant_id")) != variant_id:
                continue
            category = next(
                (key for key in CATEGORIES if _category_matches(doc, key)), "phone"
            )
            now = datetime.now(timezone.utc).isoformat()
            raw = _raw(doc, category, now)
            item = product_from_raw(raw, self.denylist, ref="?")
            if not item:
                return None
            if not self.backend:
                return replace(item, price=replace(item.price, source="catalogue"))
            facts = estate.read(
                await self.backend.product(
                    product_id,
                    (
                        estate.PRODUCT,
                        estate.PRICING,
                        estate.STOCK,
                        estate.OFFERS,
                        estate.EMI,
                    ),
                )
            )
            if facts.name:
                raw["name"] = facts.name
            spec_keys = {
                "ram": "ram",
                "internal storage": "storage",
                "usb type": "usb",
                "optical image stabilization": "ois",
            }
            for key, value in facts.specifications:
                if target := spec_keys.get(key.casefold()):
                    raw["specs"][target] = (
                        _capacity_gb(value) if target in {"ram", "storage"} else value
                    )
            raw["emi"] = {
                "state": facts.emi.state,
                "from_inr_per_month": facts.emi.per_month,
                "months": facts.emi.months,
                "provider": facts.emi.provider,
                "kind": "no_cost"
                if "no cost" in (facts.emi.kind or "").lower()
                else "standard",
                "fetched_at": now,
            }
            best = next(
                (
                    o
                    for o in facts.offers.items
                    if o.amount and o.amount == facts.offers.best_amount
                ),
                None,
            )
            if best:
                raw["offer"] = {
                    "amount_inr": best.amount,
                    "issuer_display": best.condition or best.label,
                    "stacks": facts.offers.stacks,
                    "fetched_at": now,
                }
            enriched = product_from_raw(raw, self.denylist, ref="?")
            if not enriched:
                return None
            state = (
                "unconfirmed"
                if facts.stock.in_stock is None
                else ("in_stock" if facts.stock.in_stock else "out_of_stock")
            )
            return replace(
                enriched,
                price=PriceFact(
                    facts.price.selling, facts.price.list_price, facts.price.state, now
                ),
                stock=StockFact(state, facts.stock.branches, "stores", now),
            )
        return None
