"""One question at a time, in English, for someone who does not know the specs.

Budget, brand and purpose live here, not in a sliding window of model
messages. A hard budget is a ceiling. Nothing above it is shown until the
shopper agrees to raise it, in those words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from src.chat.allowlist import (
    MANUFACTURERS,
    ModelProduct,
    benefit_line,
    for_card,
    stock_label,
)
from src.chat.copy import PRICE_UNCONFIRMED, SEARCH_DOWN, STOCK_UNCONFIRMED

_BUDGET = re.compile(
    r"(?:₹|rs\.?\s*|inr\s*)?\s*(\d{1,3}(?:,\d{2,3})+|\d{4,7}|\d{1,3}\s*k)\b",
    re.IGNORECASE,
)
_OPTION = re.compile(r"\boption\s+([a-j])\b", re.IGNORECASE)
_THE_ONE = re.compile(
    r"\bthe\s+(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+one\b",
    re.IGNORECASE,
)
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_HARD = (
    "under",
    "below",
    "within",
    "upto",
    "up to",
    "at most",
    "kulla",
    "க்குள்ள",
    "max",
)
_USB = ("usb-c", "usb c", "type-c", "type c", "usbc")
_WALK = ("walk", "walking", "moving around", "move around", "on the move")
_VIDEO = ("video", "videos", "film", "shoot", "camera")
_NONE = ("none of these", "none of them", "none of those", "இல்லை")
_SHOW_ALL = ("show all", "show me all", "all the options", "all matching", "show every")
_AVAIL = ("is this available", "is it available", "availability")
_EMI = ("emi", "installment", "instalment")
_COMPARE = ("compare",)
_YES = {"yes", "yeah", "yep", "ok", "okay", "sure", "haan", "ஆமா", "ஆம்"}
_LETTERS = "ABCDEFGHIJ"


class Catalogue(Protocol):
    async def search(
        self,
        *,
        brand: str | None,
        max_price: int | None,
    ) -> list[ModelProduct]: ...

    async def lookup(self, product_id: str, variant_id: str) -> ModelProduct | None: ...


@dataclass
class Slots:
    budget: int | None = None
    budget_hard: bool = False
    brand: str | None = None
    previous_brand: str | None = None
    purpose: str | None = None
    needs: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    visible: list[dict[str, Any]] = field(default_factory=list)
    saved: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pending: str = ""
    proposed_budget: int | None = None
    asked: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "budget_hard": self.budget_hard,
            "brand": self.brand,
            "previous_brand": self.previous_brand,
            "purpose": self.purpose,
            "needs": list(self.needs),
            "rejected": list(self.rejected),
            "visible": list(self.visible),
            "saved": {key: list(value) for key, value in self.saved.items()},
            "pending": self.pending,
            "proposed_budget": self.proposed_budget,
            "asked": list(self.asked),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Slots:
        raw = raw or {}
        return cls(
            budget=raw.get("budget"),
            budget_hard=bool(raw.get("budget_hard")),
            brand=raw.get("brand"),
            previous_brand=raw.get("previous_brand"),
            purpose=raw.get("purpose"),
            needs=list(raw.get("needs") or []),
            rejected=list(raw.get("rejected") or []),
            visible=list(raw.get("visible") or []),
            saved={key: list(value) for key, value in (raw.get("saved") or {}).items()},
            pending=str(raw.get("pending") or ""),
            proposed_budget=raw.get("proposed_budget"),
            asked=list(raw.get("asked") or []),
        )


@dataclass
class GuideTurn:
    text: str
    cards: list[dict[str, Any]]
    suggestions: list[str]
    slots: dict[str, Any]
    model_needed: bool = False
    context_label: str | None = None
    context_invalid: bool = False


def _money(token: str) -> int | None:
    cleaned = token.lower().replace(",", "").replace(" ", "")
    if cleaned.endswith("k"):
        return int(float(cleaned[:-1]) * 1000)
    if cleaned.isdigit():
        return int(cleaned)
    return None


def _find_budget(text: str) -> tuple[int | None, bool]:
    match = _BUDGET.search(text)
    if not match:
        return None, False
    amount = _money(match.group(1))
    window = text.casefold()
    hard = any(word in window for word in _HARD)
    return amount, hard or True


def _find_brand(text: str) -> str | None:
    folded = text.casefold()
    for name in sorted(MANUFACTURERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", folded):
            return name[:1].upper() + name[1:]
    return None


def _has(text: str, words: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(word in folded for word in words)


def _ordinal(text: str) -> int | None:
    match = _THE_ONE.search(text)
    if not match:
        return None
    return _ORDINALS.get(match.group(1).casefold())


def _rupees(amount: int) -> str:
    return f"₹{amount:,}"


def _need_phrase(needs: list[str]) -> str:
    parts: list[str] = []
    if "ois" in needs:
        parts.append("steady video while you walk")
    if "usb_c" in needs:
        parts.append("USB-C charging")
    return " and ".join(parts)


class ShopperGuide:
    def __init__(self, catalogue: Catalogue, *, pincode: str = "") -> None:
        self._catalogue = catalogue
        self._pincode = pincode

    async def respond(
        self,
        slots: Slots,
        text: str,
        product: dict[str, Any] | None = None,
    ) -> GuideTurn:
        cleaned = (text or "").strip()
        folded = cleaned.casefold()
        if not cleaned:
            return self._turn(
                slots, "Tell me what you are looking for, in your own words.", []
            )

        if slots.pending == "relax" and self._accepts_relax(folded, slots):
            stated, _hard = _find_budget(cleaned)
            slots.budget = stated or slots.proposed_budget or slots.budget
            slots.budget_hard = True
            slots.pending = ""
            slots.proposed_budget = None
            return await self._show(
                slots, intro="Here are options inside the budget you just agreed"
            )

        if slots.pending == "walking":
            slots.pending = ""
            if (
                _has(cleaned, _WALK)
                or folded in _YES
                or folded.startswith("yes")
                or "both" in folded
            ):
                self._add_need(slots, "ois")
            if _has(cleaned, _USB):
                self._add_need(slots, "usb_c")

        budget, hard = _find_budget(cleaned)
        if budget:
            slots.budget = budget
            slots.budget_hard = hard
        brand = _find_brand(cleaned)
        if brand and brand.casefold() != (slots.brand or "").casefold():
            if slots.brand:
                slots.previous_brand = slots.brand
            slots.brand = brand
            slots.rejected = []
            slots.pending = ""
            remembered = slots.saved.get(brand.casefold())
            slots.visible = list(remembered) if remembered else []
        if _has(cleaned, _VIDEO):
            slots.purpose = "video"
        if _has(cleaned, _USB):
            self._add_need(slots, "usb_c")
        if _has(cleaned, _WALK):
            self._add_need(slots, "ois")

        if _has(cleaned, _NONE):
            return await self._none(slots)
        if _has(cleaned, _SHOW_ALL) or folded == "show me all the options":
            return await self._show(slots, limit=10)
        if _has(cleaned, _AVAIL) or folded == "is this available?":
            return await self._available(slots, product)
        if _has(cleaned, _EMI) or folded == "emi options":
            return await self._emi(slots, product)
        if _has(cleaned, _COMPARE) and not budget and not brand:
            return await self._compare(slots, product)

        option = _OPTION.search(cleaned)
        ordinal = _ordinal(cleaned)
        if option or ordinal:
            return self._about(
                slots, option.group(1).upper() if option else "", ordinal, folded
            )

        if (
            "best phone under" in folded
            and slots.budget
            and "purpose" not in slots.asked
        ):
            slots.purpose = slots.purpose or ""
            slots.asked.append("purpose")
            slots.pending = "purpose"
            return self._turn(
                slots,
                f"A phone up to {_rupees(slots.budget)}. What will you mostly use it for — "
                "calls and messages, photos, or video?",
                ["Calls and messages", "Photos", "Video"],
            )

        if (
            slots.purpose == "video"
            and "ois" not in slots.needs
            and "walking" not in slots.asked
        ):
            slots.asked.append("walking")
            slots.pending = "walking"
            return self._turn(
                slots,
                self._ack(slots),
                ["Yes, while walking", "Mostly standing still", "Both"],
            )

        if slots.budget or slots.brand:
            intro = self._kept(slots) if brand and slots.previous_brand else ""
            return await self._show(slots, intro=intro)

        if folded in {"hi", "hello", "hey", "vanakkam"}:
            return self._turn(
                slots,
                "Tell me what you need in plain words — for example a phone and a budget.",
                ["Best phone under ₹25,000", "Compare similar"],
            )
        return GuideTurn("", [], [], slots.as_dict(), model_needed=True)

    async def _show(
        self, slots: Slots, *, intro: str = "", limit: int = 4
    ) -> GuideTurn:
        try:
            found = await self._catalogue.search(brand=slots.brand, max_price=None)
        except Exception:
            return self._turn(slots, SEARCH_DOWN, ["Try again"])
        if found is None:
            return self._turn(slots, SEARCH_DOWN, ["Try again"])
        strict = [item for item in found if self._fits(item, slots)]
        chosen = self._distinct(strict)[:limit]
        total_distinct = len(self._distinct(strict, limit=None))
        partials: list[ModelProduct] = []
        if "ois" in slots.needs and len(chosen) < 2:
            loose = [item for item in found if self._fits(item, slots, relax_ois=True)]
            for item in self._distinct(loose):
                if all(item.sku_id != have.sku_id for have in chosen):
                    partials.append(replace(item, partial=True))
                    break
        chosen = (chosen + partials)[: max(limit, len(chosen) + len(partials))]
        if not chosen:
            return await self._none(slots, searched=True)
        cards = []
        for index, item in enumerate(chosen):
            letter = _LETTERS[index]
            shown = ModelProduct(
                ref=letter,
                sku_id=item.sku_id,
                variant_id=item.variant_id,
                brand=item.brand,
                model_name=item.model_name,
                model_number=item.model_number,
                variant_label=item.variant_label,
                category=item.category,
                price=item.price,
                stock=item.stock,
                specs=item.specs,
                offer=item.offer,
                emi=item.emi,
                partial=item.partial,
            )
            cards.append(for_card(shown, self._pincode))
        slots.visible = cards
        if slots.brand:
            slots.saved[slots.brand.casefold()] = list(cards)
        count = len(cards)
        need = _need_phrase(slots.needs)
        brand = slots.brand or "matching"
        ceiling = f" within {_rupees(slots.budget)}" if slots.budget else ""
        detail = f" that have {need}" if need else ""
        lead = intro.strip()
        if lead:
            lead = lead.rstrip(".") + ". "
        more = ""
        if limit >= 10 and total_distinct > 10:
            more = " Showing the top 10. Narrow by price or feature to see others."
        elif any(card.get("partial") for card in cards):
            more = (
                " One of these is shown because it is the only other match under your budget, "
                "and it steadies the picture in software rather than with the lens. "
                "Does the stabilisation need to be optical, or is electronic OK?"
            )
        else:
            more = " Want me to compare these for low-light video, or show all matching phones?"
        text = (
            f"{lead}Here are {count} {brand} phones{ceiling}{detail}.{more}"
        ).replace("  ", " ")
        suggestions = ["Show all matching phones", "Compare these"]
        if any(card.get("partial") for card in cards):
            suggestions = ["Optical only", "Electronic is OK"]
        return self._turn(slots, text.strip(), suggestions, cards)

    async def _none(self, slots: Slots, *, searched: bool = False) -> GuideTurn:
        for card in slots.visible:
            sku = str(card.get("sku_id") or "")
            if sku and sku not in slots.rejected:
                slots.rejected.append(sku)
        slots.visible = []
        proposal = (slots.budget or 0) + 2000 if slots.budget else None
        if slots.budget:
            try:
                found = await self._catalogue.search(brand=slots.brand, max_price=None)
            except Exception:
                found = []
            higher = [
                item.price.selling_inr
                for item in found or []
                if item.price.selling_inr
                and slots.budget < item.price.selling_inr <= slots.budget + 5000
            ]
            if higher:
                proposal = max(higher)
        slots.proposed_budget = proposal
        slots.pending = "relax" if proposal and slots.budget_hard else ""
        brand = slots.brand or "that brand"
        ceiling = _rupees(slots.budget) if slots.budget else "your budget"
        suggestions: list[str] = []
        extra = ""
        if proposal and slots.budget_hard:
            suggestions.append(f"Show {brand} up to {_rupees(proposal)}")
            extra = f" Your {ceiling} limit stays unless you choose otherwise."
        if slots.previous_brand:
            suggestions.append(f"Go back to {slots.previous_brand}")
        if "ois" in slots.needs:
            suggestions.append("Allow electronic stabilisation")
        if not searched:
            lead = "Understood, I've set those aside. "
        else:
            lead = ""
        text = (
            f"{lead}There's no other {brand} matching that under {ceiling}. "
            f"Would you like to look a little wider?{extra}"
        )
        return self._turn(slots, " ".join(text.split()), suggestions, [])

    async def _available(
        self, slots: Slots, product: dict[str, Any] | None
    ) -> GuideTurn:
        if not product:
            return self._turn(
                slots,
                "Open a product page and ask again, and I'll check that exact model.",
                [],
            )
        found = await self._safe_lookup(
            str(product.get("product_id") or ""), str(product.get("variant_id") or "")
        )
        if found is None:
            slots_label = "this page"
            return GuideTurn(
                "I couldn't match this page to a product I can check. Ask me about a phone and a budget instead.",
                [],
                ["Best phone under ₹25,000"],
                slots.as_dict(),
                context_label=slots_label,
                context_invalid=True,
            )
        label = stock_label(found.stock, self._pincode)
        price = (
            _rupees(found.price.selling_inr)
            if found.price.state == "known" and found.price.selling_inr
            else PRICE_UNCONFIRMED
        )
        name = f"{found.model_name}, {found.variant_label}".strip(", ")
        if found.stock.state == "unconfirmed":
            text = f"{name}: {STOCK_UNCONFIRMED} {price}."
        elif found.stock.state == "out_of_stock":
            text = f"This {name} is out of stock near you and online."
            sibling = await self._in_stock_sibling(found)
            if sibling is not None and sibling.price.selling_inr:
                text += (
                    f" The {sibling.variant_label} version is "
                    f"{stock_label(sibling.stock, self._pincode).lower()} for "
                    f"{_rupees(sibling.price.selling_inr)}."
                )
        else:
            text = f"This {name} is {label.lower()} for {price}."
        card = for_card(
            ModelProduct(
                ref="A",
                sku_id=found.sku_id,
                variant_id=found.variant_id,
                brand=found.brand,
                model_name=found.model_name,
                model_number=found.model_number,
                variant_label=found.variant_label,
                category=found.category,
                price=found.price,
                stock=found.stock,
                specs=found.specs,
                offer=found.offer,
                emi=found.emi,
            ),
            self._pincode,
        )
        slots.visible = [card]
        return GuideTurn(
            text,
            [card],
            ["Check again"] if found.stock.state == "unconfirmed" else ["EMI options"],
            slots.as_dict(),
            context_label=found.model_name,
        )

    async def _emi(self, slots: Slots, product: dict[str, Any] | None) -> GuideTurn:
        card = None
        if slots.visible:
            card = slots.visible[0]
        if product and not card:
            found = await self._safe_lookup(
                str(product.get("product_id") or ""),
                str(product.get("variant_id") or ""),
            )
            if found is not None:
                card = for_card(found, self._pincode)
        if card is None:
            return self._turn(
                slots, "Tell me which phone you want EMI for — option A, B, or C.", []
            )
        line = card.get("emi_line")
        offer = card.get("offer_line")
        if not line and not offer:
            return self._turn(
                slots, "Offer and EMI details couldn't be confirmed.", ["Check again"]
            )
        bits = [f"{card.get('model_name')} ({card.get('ref')})"]
        price = (card.get("price") or {}).get("selling_inr")
        if price:
            bits.append(_rupees(int(price)))
        if line:
            bits.append(str(line))
        if offer:
            bits.append(str(offer))
        bits.append(
            "I only quote EMI the bank already published. I don't work it out myself."
        )
        return self._turn(slots, ": ".join(bits[:2]) + ". " + ". ".join(bits[2:]), [])

    async def _compare(self, slots: Slots, product: dict[str, Any] | None) -> GuideTurn:
        if len(slots.visible) >= 2:
            names = ", ".join(
                f"{card.get('ref')} {card.get('model_name')}"
                for card in slots.visible[:4]
            )
            return self._turn(
                slots,
                f"On screen now: {names}. Tell me what matters more — night video, battery, or price.",
                ["Night video", "Battery", "Price"],
            )
        if product:
            found = await self._safe_lookup(
                str(product.get("product_id") or ""),
                str(product.get("variant_id") or ""),
            )
            if found is not None:
                slots.brand = found.brand or slots.brand
                if found.price.selling_inr and not slots.budget:
                    slots.budget = found.price.selling_inr + 3000
                    slots.budget_hard = True
                return await self._show(
                    slots, intro=f"Phones similar to {found.model_name}"
                )
        return self._turn(
            slots,
            "Show me two phones first, or open a product, and I'll compare like for like.",
            ["Best phone under ₹25,000"],
        )

    def _about(
        self, slots: Slots, letter: str, ordinal: int | None, folded: str
    ) -> GuideTurn:
        index = _LETTERS.index(letter) if letter in _LETTERS else (ordinal or 1) - 1
        if index < 0 or index >= len(slots.visible):
            return self._turn(
                slots,
                "I can only talk about the phones on screen. Ask for option A, B, or C.",
                [],
            )
        card = slots.visible[index]
        if "emi" in folded:
            slots.visible = [card] + [
                other for other in slots.visible if other is not card
            ]
            line = (
                card.get("emi_line") or "Offer and EMI details couldn't be confirmed."
            )
            offer = card.get("offer_line")
            text = f"{card.get('model_name')} ({card.get('ref')}): {line}"
            if offer:
                text += f" {offer}"
            return self._turn(slots, text, [])
        price = (card.get("price") or {}).get("selling_inr")
        price_text = _rupees(int(price)) if price else PRICE_UNCONFIRMED
        stock = (card.get("stock") or {}).get(
            "label"
        ) or "Stock not confirmed right now"
        benefit = card.get("benefit") or ""
        text = (
            f"{card.get('model_name')} ({card.get('ref')}), {card.get('variant_label')}, "
            f"{price_text}: {stock}."
        )
        if benefit:
            text += f" {benefit}."
        return self._turn(
            slots, " ".join(text.split()), ["EMI options", "Is this available?"]
        )

    def _fits(
        self, item: ModelProduct, slots: Slots, *, relax_ois: bool = False
    ) -> bool:
        if item.sku_id and item.sku_id in slots.rejected:
            return False
        if slots.brand and item.brand.casefold() != slots.brand.casefold():
            return False
        if slots.budget and slots.budget_hard:
            if item.price.state != "known" or not item.price.selling_inr:
                return False
            if item.price.selling_inr > slots.budget:
                return False
        if "usb_c" in slots.needs and item.specs.usb != "usb_c":
            return False
        if not relax_ois and "ois" in slots.needs and item.specs.ois is not True:
            return False
        return True

    def _distinct(
        self, products: list[ModelProduct], *, limit: int | None = 10
    ) -> list[ModelProduct]:
        seen: set[tuple[str, int | None, int | None]] = set()
        chosen: list[ModelProduct] = []
        for item in products:
            key = (
                item.model_number or item.model_name,
                item.specs.ram_gb,
                item.specs.storage_gb,
            )
            if key in seen:
                continue
            seen.add(key)
            chosen.append(item)
            if limit is not None and len(chosen) == limit:
                break
        return chosen

    def _ack(self, slots: Slots) -> str:
        brand = slots.brand or "a"
        ceiling = f" up to {_rupees(slots.budget)}" if slots.budget else ""
        purpose = " for recording videos on site" if slots.purpose == "video" else ""
        return (
            f"Got it: a {brand} phone{ceiling}{purpose}. Do you usually record while "
            "walking or moving around? If so, I'll prioritise steady video while you walk."
        )

    def _kept(self, slots: Slots) -> str:
        brand = slots.brand or "that brand"
        ceiling = f" up to {_rupees(slots.budget)}" if slots.budget else ""
        need = _need_phrase(slots.needs)
        extra = f" with {need}" if need else ""
        purpose = (
            " for walking videos"
            if slots.purpose == "video" and "ois" in slots.needs
            else ""
        )
        return f"Switching to {brand}, still{ceiling}{extra}{purpose}"

    def _accepts_relax(self, folded: str, slots: Slots) -> bool:
        if folded in _YES or folded.startswith("yes") or folded.startswith("show "):
            return True
        if slots.proposed_budget and str(slots.proposed_budget) in folded.replace(
            ",", ""
        ):
            return True
        return False

    def _add_need(self, slots: Slots, need: str) -> None:
        if need not in slots.needs:
            slots.needs.append(need)

    async def _safe_lookup(
        self, product_id: str, variant_id: str
    ) -> ModelProduct | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", product_id or ""):
            return None
        if variant_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", variant_id):
            return None
        try:
            return await self._catalogue.lookup(product_id, variant_id)
        except Exception:
            return None

    async def _in_stock_sibling(self, product: ModelProduct) -> ModelProduct | None:
        try:
            found = await self._catalogue.search(brand=product.brand, max_price=None)
        except Exception:
            return None
        for item in found or []:
            if (
                item.model_name == product.model_name
                and item.variant_id != product.variant_id
            ):
                if item.stock.state == "in_stock":
                    return item
        return None

    def _turn(
        self,
        slots: Slots,
        text: str,
        suggestions: list[str],
        cards: list[dict[str, Any]] | None = None,
    ) -> GuideTurn:
        return GuideTurn(text, cards or [], suggestions, slots.as_dict())


def describe_benefit(product: ModelProduct) -> str:
    return benefit_line(product.specs)
