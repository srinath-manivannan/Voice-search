"""The only product fields a model reply is allowed to see.

Upstream copy, URLs, seller names and error text stop here. What crosses
into model history is a ``ModelProduct``. The same deny-list runs again on
the finished English reply, so a description that survived the first pass
cannot come back out through the model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Manufacturers a product may be labelled with. Free text is not a brand:
# a poisoned name would otherwise ride into the prompt as one.
MANUFACTURERS: frozenset[str] = frozenset(
    {
        "samsung",
        "vivo",
        "apple",
        "xiaomi",
        "redmi",
        "poco",
        "oppo",
        "realme",
        "oneplus",
        "motorola",
        "nothing",
        "google",
        "sony",
        "lg",
        "nokia",
        "iqoo",
        "infinix",
        "tecno",
        "honor",
        "lenovo",
        "hp",
        "dell",
        "asus",
        "acer",
        "msi",
        "boat",
        "jbl",
        "bose",
        "philips",
        "whirlpool",
        "godrej",
        "voltas",
        "haier",
        "panasonic",
        "tcl",
        "hisense",
    }
)

CATEGORIES = frozenset(
    {"phone", "tv", "laptop", "ac", "fridge", "audio", "tablet", "watch"}
)

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.\w+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)")
_HTML = re.compile(r"<[^>]+>")
_MODEL_NUMBER = re.compile(r"^[A-Za-z0-9 ./+-]{2,32}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_TAMIL = re.compile(r"[\u0B80-\u0BFF]")
_LATIN = re.compile(r"[A-Za-z]")

# Tool failures the model may be told about. Anything else, including a
# message that quotes a hostname, becomes the generic code.
_ERROR_CODES = {
    "catalogue_unavailable": "catalogue_unavailable",
    "catalogue_not_configured": "catalogue_unavailable",
    "details_unavailable": "details_unavailable",
    "no_such_product": "no_such_product",
    "offers_unavailable": "offers_unavailable",
    "inventory_unavailable": "inventory_unavailable",
}

_STOCK_STATES = {"in_stock": "in_stock", "out_of_stock": "out_of_stock"}


def parse_denylist(raw: str) -> tuple[str, ...]:
    """Comma or newline separated tokens. Empty stays empty.

    The list is a deployment secret. It is never written into the repo, and
    a missing list means the filter has nothing to remove — the structural
    allowlist still drops URLs, addresses and unknown keys.
    """
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        token = line.strip().casefold()
        if token:
            parts.append(token)
    return tuple(dict.fromkeys(parts))


def _scrub(value: str, denylist: tuple[str, ...], limit: int) -> str:
    text = unicodedata.normalize("NFC", value or "")
    text = _HTML.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _EMAIL.sub(" ", text)
    text = _PHONE.sub(" ", text)
    folded = text.casefold()
    for token in denylist:
        if token and token in folded:
            text = re.sub(re.escape(token), " ", text, flags=re.IGNORECASE)
            folded = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def hits_denylist(text: str, denylist: tuple[str, ...]) -> bool:
    folded = (text or "").casefold()
    return any(token and token in folded for token in denylist)


def is_english_display(text: str, allowed: tuple[str, ...] = ()) -> bool:
    """Assistant text is English. Shopper brand and model tokens may stay.

    Tamil script is how a screen reader would switch voice, and it is not
    allowed in anything the assistant wrote. A brand the shopper typed is
    removed before the check so a Latin name is not the thing that fails.
    """
    sample = text or ""
    for token in allowed:
        if token:
            sample = re.sub(re.escape(token), " ", sample, flags=re.IGNORECASE)
    if _TAMIL.search(sample):
        return False
    letters = _LATIN.findall(sample)
    return bool(letters) or not re.search(r"[^\W\d_]", sample, flags=re.UNICODE)


@dataclass(frozen=True)
class PriceFact:
    selling_inr: int | None
    list_inr: int | None
    state: str
    fetched_at: str
    source: str = "pricing"

    def as_dict(self) -> dict[str, Any]:
        return {
            "selling_inr": self.selling_inr,
            "list_inr": self.list_inr,
            "state": self.state,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class StockFact:
    state: str
    stores_nearby: int | None
    channel: str
    fetched_at: str
    source: str = "central_inventory"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "stores_nearby": self.stores_nearby,
            "channel": self.channel,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class Specs:
    camera_mp: int | None = None
    ois: bool | None = None
    video_max: str | None = None
    battery_mah: int | None = None
    ram_gb: int | None = None
    storage_gb: int | None = None
    display_in: float | None = None
    five_g: bool | None = None
    usb: str | None = None
    weight_g: int | None = None
    colour: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_mp": self.camera_mp,
            "ois": self.ois,
            "video_max": self.video_max,
            "battery_mah": self.battery_mah,
            "ram_gb": self.ram_gb,
            "storage_gb": self.storage_gb,
            "display_in": self.display_in,
            "five_g": self.five_g,
            "usb": self.usb,
            "weight_g": self.weight_g,
        }


@dataclass(frozen=True)
class OfferFact:
    kind: str
    amount_inr: int
    issuer_display: str
    condition_code: str
    stacks: bool
    state: str
    fetched_at: str
    source: str = "offers"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount_inr": self.amount_inr,
            "issuer_display": self.issuer_display,
            "condition_code": self.condition_code,
            "stacks": self.stacks,
            "state": self.state,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class EmiFact:
    kind: str
    from_inr_per_month: int
    months: int
    issuer_display: str
    state: str
    fetched_at: str
    source: str = "emi"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from_inr_per_month": self.from_inr_per_month,
            "months": self.months,
            "issuer_display": self.issuer_display,
            "state": self.state,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class ModelProduct:
    """One sellable variant, after the allowlist.

    ``sku_id`` and ``variant_id`` stay on the server and on the card the
    shopper taps. The model is shown ``ref`` only.
    """

    ref: str
    sku_id: str
    variant_id: str
    brand: str
    model_name: str
    model_number: str
    variant_label: str
    category: str
    price: PriceFact
    stock: StockFact
    specs: Specs
    offer: OfferFact | None = None
    emi: EmiFact | None = None
    partial: bool = False

    def allowed_tokens(self) -> tuple[str, ...]:
        tokens = [self.brand, self.model_name, self.model_number, self.variant_label]
        return tuple(token for token in tokens if token)

    def for_model(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ref": self.ref,
            "brand": self.brand,
            "model_name": self.model_name,
            "model_number": self.model_number,
            "variant_label": self.variant_label,
            "category": self.category,
            "price": self.price.as_dict(),
            "stock": {
                "state": self.stock.state,
                "stores_nearby": self.stock.stores_nearby,
                "fetched_at": self.stock.fetched_at,
                "source": self.stock.source,
            },
            "specs": self.specs.as_dict(),
            "offer": self.offer.as_dict() if self.offer else None,
            "emi": self.emi.as_dict() if self.emi else None,
        }
        return body


def stock_label(stock: StockFact, pincode: str = "") -> str:
    """Shopper-facing stock. Unconfirmed is never called out of stock."""
    if stock.state == "out_of_stock":
        if pincode:
            return f"Out of stock near {pincode}"
        return "Out of stock"
    if stock.state != "in_stock":
        return "Stock not confirmed right now"
    if stock.channel == "catalogue":
        return "Listed in stock · store availability unconfirmed"
    if stock.channel == "stores":
        return (
            f"In stock at {stock.stores_nearby} stores"
            if stock.stores_nearby
            else "In stock"
        )
    if stock.channel == "online" or stock.stores_nearby == 0:
        return "Available online"
    if stock.stores_nearby:
        return f"In stock at {stock.stores_nearby} stores near you"
    return "In stock near you"


def benefit_line(specs: Specs) -> str:
    """Specs as something a first-time shopper can use."""
    parts: list[str] = []
    if specs.ois is True:
        parts.append("Steady video while you walk")
    elif specs.ois is False:
        parts.append("Electronic stabilisation only")
    if specs.video_max:
        parts.append(f"{specs.video_max} video")
    if specs.usb == "usb_c":
        parts.append("USB-C charging")
    if specs.battery_mah and specs.battery_mah >= 4500:
        parts.append("Battery built for a full day")
    if specs.five_g:
        parts.append("Works on 5G networks")
    return " · ".join(parts)


def offer_line(offer: OfferFact | None) -> str | None:
    if offer is None or offer.state != "known" or offer.amount_inr <= 0:
        return None
    issuer = offer.issuer_display or "the listed card"
    stacks = "" if offer.stacks else " Offers don't combine."
    return f"₹{offer.amount_inr:,} off with {issuer}.{stacks}"


def emi_line(emi: EmiFact | None) -> str | None:
    if emi is None or emi.state != "known" or not emi.from_inr_per_month:
        return None
    kind = "No-cost EMI" if emi.kind == "no_cost" else "EMI"
    issuer = f" on {emi.issuer_display}" if emi.issuer_display else ""
    return (
        f"{kind}{issuer} for {emi.months} months from ₹{emi.from_inr_per_month:,}/month"
    )


def for_card(product: ModelProduct, pincode: str = "") -> dict[str, Any]:
    price_state = (
        product.price.state
        if product.price.state in {"known", "unknown"}
        else "unknown"
    )
    selling = product.price.selling_inr if price_state == "known" else None
    return {
        "ref": product.ref,
        "sku_id": product.sku_id,
        "variant_id": product.variant_id,
        "model_name": product.model_name,
        "variant_label": product.variant_label,
        "brand": product.brand,
        "benefit": benefit_line(product.specs),
        "partial": product.partial,
        "price": {
            "selling_inr": selling,
            "state": price_state,
            "source": product.price.source,
            "fetched_at": product.price.fetched_at,
        },
        "stock": {
            "state": product.stock.state
            if product.stock.state in {"in_stock", "out_of_stock", "unconfirmed"}
            else "unconfirmed",
            "label": stock_label(product.stock, pincode),
            "stores_nearby": product.stock.stores_nearby,
            "source": product.stock.source,
            "fetched_at": product.stock.fetched_at,
        },
        "offer_line": offer_line(product.offer),
        "emi_line": emi_line(product.emi),
        "actions": ["view", "compare", "emi"],
    }


def guard_display(
    text: str,
    denylist: tuple[str, ...],
    fallback: str,
    allowed: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Return the reply, or the neutral fallback when it is not safe to show."""
    if hits_denylist(text, denylist) or not is_english_display(text, allowed):
        return fallback, True
    return text, False


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in {"yes", "true", "1", "ois"}:
            return True
        if folded in {"no", "false", "0"}:
            return False
    return None


def _specs_from(raw: Any, denylist: tuple[str, ...]) -> Specs:
    if not isinstance(raw, dict):
        return Specs()
    usb = raw.get("usb") or raw.get("usb_type")
    usb_code = None
    if isinstance(usb, str) and "c" in usb.casefold() and "micro" not in usb.casefold():
        usb_code = "usb_c"
    elif isinstance(usb, str) and "micro" in usb.casefold():
        usb_code = "micro_usb"
    colour = raw.get("colour") or raw.get("color")
    colour_text = _scrub(colour, denylist, 32) if isinstance(colour, str) else None
    video = raw.get("video_max") or raw.get("video")
    video_text = _scrub(video, denylist, 16) if isinstance(video, str) else None
    return Specs(
        camera_mp=_int(raw.get("camera_mp")),
        ois=_boolish(raw.get("ois")),
        video_max=video_text or None,
        battery_mah=_int(raw.get("battery_mah")),
        ram_gb=_int(raw.get("ram_gb") or raw.get("ram")),
        storage_gb=_int(raw.get("storage_gb") or raw.get("storage")),
        display_in=None,
        five_g=_boolish(raw.get("five_g")),
        usb=usb_code,
        weight_g=_int(raw.get("weight_g")),
        colour=colour_text or None,
    )


def _variant_label(specs: Specs, fallback: str, denylist: tuple[str, ...]) -> str:
    if specs.ram_gb and specs.storage_gb:
        colour = specs.colour or ""
        label = f"{specs.ram_gb} GB + {specs.storage_gb} GB"
        if colour:
            label = f"{label}, {colour}"
        return label
    return _scrub(fallback, denylist, 80)


def _stock_from(raw: Any) -> StockFact:
    fetched = ""
    if isinstance(raw, dict):
        state_raw = str(raw.get("state") or raw.get("status") or "").casefold()
        fetched = str(raw.get("fetched_at") or "")[:40]
        available = raw.get("available")
        stores = _int(
            raw.get("stores_nearby") if "stores_nearby" in raw else raw.get("branches")
        )
        channel = str(raw.get("channel") or "")
        if state_raw in {"unknown", "unconfirmed", "timeout", "error"}:
            return StockFact("unconfirmed", None, "unknown", fetched)
        if state_raw == "out_of_stock" or available is False:
            return StockFact("out_of_stock", stores or 0, "nearby", fetched)
        if state_raw in {"in_stock", "known"} or available is True:
            if stores == 0 or channel == "online":
                return StockFact("in_stock", 0, "online", fetched)
            return StockFact("in_stock", stores, "nearby", fetched)
        if "in_stock_hint" in raw and raw.get("in_stock_hint") is None:
            return StockFact("unconfirmed", None, "unknown", fetched)
    return StockFact("unconfirmed", None, "unknown", fetched)


def _price_from(raw: Any) -> PriceFact:
    if not isinstance(raw, dict) and not isinstance(raw, int):
        return PriceFact(None, None, "unknown", "", "pricing")
    if isinstance(raw, int):
        return PriceFact(raw, None, "known", "", "pricing")
    selling = _int(
        raw.get("selling_inr") if "selling_inr" in raw else raw.get("price_inr")
    )
    state = str(raw.get("state") or ("known" if selling else "unknown"))
    if state not in {"known", "unknown"}:
        state = "known" if selling else "unknown"
    if state != "known":
        selling = None
    return PriceFact(
        selling,
        _int(raw.get("list_inr") or raw.get("was_inr")),
        state,
        str(raw.get("fetched_at") or "")[:40],
    )


def _offer_from(raw: Any, denylist: tuple[str, ...]) -> OfferFact | None:
    if not isinstance(raw, dict):
        return None
    state = str(raw.get("state") or "known")
    if state != "known":
        return None
    amount = _int(raw.get("amount_inr"))
    if not amount:
        return None
    issuer = _scrub(
        str(raw.get("issuer_display") or raw.get("only_with") or ""), denylist, 40
    )
    return OfferFact(
        kind="card_discount",
        amount_inr=amount,
        issuer_display=issuer,
        condition_code=str(raw.get("condition_code") or "card"),
        stacks=bool(raw.get("stacks")),
        state="known",
        fetched_at=str(raw.get("fetched_at") or "")[:40],
    )


def _emi_from(raw: Any, denylist: tuple[str, ...]) -> EmiFact | None:
    if not isinstance(raw, dict):
        return None
    state = str(raw.get("state") or "known")
    if state != "known":
        return None
    monthly = _int(raw.get("from_inr_per_month"))
    months = _int(raw.get("months"))
    if not monthly or not months:
        return None
    issuer = _scrub(
        str(raw.get("issuer_display") or raw.get("provider") or ""), denylist, 40
    )
    kind = "no_cost" if str(raw.get("kind") or "") == "no_cost" else "standard"
    return EmiFact(
        kind, monthly, months, issuer, "known", str(raw.get("fetched_at") or "")[:40]
    )


def product_from_raw(
    raw: dict[str, Any],
    denylist: tuple[str, ...],
    *,
    ref: str,
) -> ModelProduct | None:
    """Build one product, or None when the row has no name left after scrubbing."""
    brand_raw = str(raw.get("brand") or "")
    brand = brand_raw if brand_raw.casefold() in MANUFACTURERS else ""
    brand = _scrub(brand, denylist, 40)
    number_raw = str(raw.get("model_number") or "")
    model_number = number_raw if _MODEL_NUMBER.match(number_raw) else ""
    name = _scrub(str(raw.get("model_name") or raw.get("name") or ""), denylist, 80)
    if not name:
        name = model_number
    if not name:
        return None
    specs = _specs_from(
        raw.get("specs") if isinstance(raw.get("specs"), dict) else raw, denylist
    )
    label = _variant_label(specs, str(raw.get("variant_label") or ""), denylist)
    category = str(raw.get("category") or "phone").casefold()
    if category not in CATEGORIES:
        category = "phone"
    sku = str(raw.get("sku_id") or raw.get("item_code") or "")
    variant = str(raw.get("variant_id") or "")
    if sku and not _ID.match(sku):
        sku = ""
    if variant and not _ID.match(variant):
        variant = ""
    price_raw = raw.get("price") if isinstance(raw.get("price"), dict) else raw
    stock_raw = raw.get("stock") if isinstance(raw.get("stock"), dict) else {}
    if not stock_raw and "in_stock_hint" in raw:
        stock_raw = {"available": raw.get("in_stock_hint")}
    return ModelProduct(
        ref=ref,
        sku_id=sku,
        variant_id=variant,
        brand=brand,
        model_name=name,
        model_number=model_number or name[:32],
        variant_label=label,
        category=category,
        price=_price_from(price_raw),
        stock=_stock_from(stock_raw),
        specs=specs,
        offer=_offer_from(raw.get("offer") or raw.get("best_offer"), denylist),
        emi=_emi_from(raw.get("emi"), denylist),
    )


def sanitise_tool_result(
    result: dict[str, Any], denylist: tuple[str, ...]
) -> dict[str, Any]:
    """Drop everything the model must not see. Errors become an enum."""
    if not isinstance(result, dict):
        return {"error": "details_unavailable"}
    error = result.get("error")
    if error:
        code = _ERROR_CODES.get(str(error), "details_unavailable")
        return {"error": code}
    products = []
    rows = result.get("products")
    if isinstance(rows, list):
        letters = "ABCDEFGHIJ"
        for index, row in enumerate(rows[:10]):
            if not isinstance(row, dict):
                continue
            built = product_from_raw(row, denylist, ref=letters[index])
            if built is not None:
                products.append(built.for_model())
    cleaned: dict[str, Any] = {"products": products}
    if result.get("nothing_inside_budget") is True:
        cleaned["nothing_inside_budget"] = True
    return cleaned


def neutral_fallback(products: list[ModelProduct], pincode: str = "") -> str:
    """A reply built only from allowlisted facts, used when the model slips."""
    if not products:
        return (
            "I can help you compare phones, prices and stock in plain English. "
            "Tell me a budget and what you will use it for."
        )
    lines = ["Here is what I can confirm:"]
    for product in products[:4]:
        price = (
            f"₹{product.price.selling_inr:,}"
            if product.price.state == "known" and product.price.selling_inr
            else "price not confirmed"
        )
        lines.append(
            f"{product.ref}. {product.model_name}, {price}. {stock_label(product.stock, pincode)}."
        )
    return " ".join(lines)
