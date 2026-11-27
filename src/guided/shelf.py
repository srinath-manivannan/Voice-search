"""What is really on a shelf: live stock, and the price in force.

The guided call narrows a list, so the list has to be true before the first
question is asked. The search index is not that list. Measured 2026-11-23,
with the index pointed at staging and the estate at production:

    phones the index calls in stock          70
    of those, in stock at a branch           16
    index price of a Realme P4 Lite 5G   14,499
    what it sells for                    20,999

A budget button built on the index would have offered that phone under
15,000 and then had nothing to show for it; "20-30k" found no phone at all
when two were on the shelf. So the buttons are not built on the index. Each
shelf is read from it once, every item is asked of the estate, and what the
engine narrows is what survived: in stock, at a price somebody will be
charged.

That costs a burst of calls to the retailer's own services - 86 for the phone
shelf, about seven seconds cold - which is why it is done once per shelf and
kept for a quarter of an hour, not once per customer. Stock first, and only
the survivors are priced: 54 of those 70 phones never needed a price.

What is kept is small on purpose. A product's facts for a card, the values
its shelf can be narrowed on, and the numbers a priority is judged by.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from src.agent.tools import estate as estate_api

log = logging.getLogger(__name__)

# How long a shelf is believed. Stock moves in minutes, not seconds, and the
# one product a customer actually picks is asked again, live, before they
# are shown its card - so this bounds a button being stale, never a sale.
TTL_SECONDS = 900.0
# After a refresh fails, how long before trying again. Without it every
# customer during an outage would start their own rebuild.
RETRY_SECONDS = 60.0
# Calls in flight at once for a background build. The estate's own pool is
# twenty-four connections; a build takes eight, so a customer's tap - their
# card's live price, stock and EMI - never queues behind a rebuild
# (measured 2026-11-24: 2-4 s card waits while a shelf rebuilt at twelve).
WIDE = 8
# One item that will not answer does not hold the shelf.
PATIENCE_SECONDS = 8.0
# A shelf saved to disk is served at once after a restart, and rebuilt
# behind it - unless it is older than this, when it is too old to show.
SNAPSHOT_MAX_AGE_SECONDS = 6 * 3600.0
# 2: phones carry their kind (keypad or not); 3: and their PIM features.
SNAPSHOT_VERSION = 3
# How long a question waits for a first-ever build before the index's own
# shelf answers it instead. Short shelves are verified inside this; only a
# long build (630 phones) hands the questions to the index meanwhile.
FIRST_WAIT_SECONDS = 1.5

TEXT, FLOOR, RANGE = "text", "floor", "range"


class Index(Protocol):
    """The slice of the catalogue a shelf is read from."""

    async def shelf(self, category: str, limit: int = ...) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Spec:
    """One line on a card: a Tamil label and where its value is read from."""

    label: str
    source: str
    style: str = TEXT
    unit: str = ""


@dataclass(frozen=True)
class Item:
    """One product a customer could walk out with."""

    item_code: str
    name: str
    brand: str | None
    price: int
    was: int | None = None
    specs: tuple[tuple[str, str], ...] = ()
    # What the shelf can be narrowed on: brand, and whatever its script asks.
    attributes: dict[str, str] = field(default_factory=dict)
    # What a priority is judged by. Numbers, so they can be ordered.
    measures: dict[str, float] = field(default_factory=dict)
    rating: float | None = None
    quantity: int | None = None
    branches: int | None = None
    # False means "we could not ask", and the card says so. It never means no.
    stock_confirmed: bool = False
    price_confirmed: bool = False
    # The same product in this many colours, shown once.
    colours: int = 1
    checked_at: int = 0
    pim_fields: tuple[str, ...] = ()


# The lines a card may carry, per shelf, in the order they are shown. A
# handful each: the name already says most of it, and a card is read on a
# phone held in one hand.
SPECS: dict[str, tuple[Spec, ...]] = {
    "phone": (
        Spec("கேமரா", "primary_camera", FLOOR, "MP"),
        Spec("பேட்டரி", "battery_capacity", FLOOR, "mAh"),
        Spec("RAM", "ram"),
        Spec("ஸ்டோரேஜ்", "internal_storage"),
        Spec("டிஸ்ப்ளே", "display_technology"),
        Spec("நெட்வொர்க்", "cellulartechnology"),
    ),
    "tv": (
        Spec("ஸ்க்ரீன்", "screen_size", RANGE),
        Spec("ரெசல்யூஷன்", "resolution_type"),
        Spec("டிஸ்ப்ளே", "display_technology"),
        Spec("OS", "operating_system"),
    ),
    "laptop": (
        Spec("ப்ராசஸர்", "processor"),
        Spec("RAM", "ram"),
        Spec("SSD", "ssd_capacity"),
        Spec("ஸ்க்ரீன்", "screen_size", RANGE),
    ),
    "ac": (
        Spec("கெபாசிட்டி", "capacity", RANGE),
        Spec("ஸ்டார்", "energy_rating"),
        Spec("டெக்னாலஜி", "technology"),
        Spec("வகை", "type"),
    ),
    "fridge": (
        Spec("கெபாசிட்டி", "capacity", RANGE),
        Spec("வகை", "type"),
        Spec("கூலிங்", "cooling_technology"),
        Spec("ஸ்டார்", "energy_rating"),
    ),
    "washing machine": (
        Spec("கெபாசிட்டி", "washer_capacity", RANGE),
        Spec("வகை", "load_orientation"),
        Spec("ஆபரேஷன்", "operation"),
        Spec("ஸ்டார்", "energy_rating"),
    ),
    "tablet": (
        Spec("ஸ்டோரேஜ்", "internal_storage"),
        Spec("ஸ்க்ரீன்", "screen_size", RANGE),
        Spec("OS", "operating_system"),
    ),
    "watch": (Spec("டிஸ்ப்ளே", "display_type"), Spec("டயல்", "dial_shape")),
    "headphones": (Spec("வகை", "type"),),
    "speaker": (Spec("சேனல்", "configuration"),),
}

# What a shelf's script may narrow on, beyond brand. The key is the id of
# the `Facet` in `script.py`; the value is the field it reads and how.
ATTRIBUTES: dict[str, dict[str, tuple[str, str]]] = {
    "tv": {"size": ("screen_size", RANGE)},
    "ac": {"capacity": ("capacity", RANGE)},
    "fridge": {"kind": ("type", TEXT)},
    "washing machine": {"kind": ("load_orientation", TEXT)},
    "headphones": {"kind": ("type", TEXT)},
}

_GIGABYTES = "gb"
# What a priority is ordered by. The key is `Priority.measure`.
MEASURES: dict[str, dict[str, tuple[str, str]]] = {
    "phone": {
        "camera_mp": ("primary_camera", FLOOR),
        "battery_mah": ("battery_capacity", FLOOR),
        "ram_gb": ("ram", _GIGABYTES),
        "storage_gb": ("internal_storage", _GIGABYTES),
    },
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# "Less than 2499 mAh" has a number in it and no floor. Reading that number
# as one would print "2499mAh+" on a phone the index says is below it.
_CEILING = re.compile(r"less than|below|up ?to|under", re.IGNORECASE)
_ABOVE = re.compile(r"(?:&|and)\s*above|^over\b|^above\b", re.IGNORECASE)
_SPAN = re.compile(
    r"^\s*([\d.]+)\s*([a-z]*)\s*(?:-|–|to)\s*([\d.]+)\s*([a-z]*)\s*$", re.IGNORECASE
)
_SIZE = re.compile(r"([\d.]+)\s*(tb|gb|mb)", re.IGNORECASE)
_UNITS = {"inches": "inch", "in": "inch", "inch": "inch"}


def _first(value: Any) -> str | None:
    """These fields are single-element lists more often than they are strings."""
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value if value is not None else "").strip()
    return text or None


def _low(text: str) -> float | None:
    """The bottom of a band, or None when the band has no bottom."""
    if _CEILING.search(text):
        return None
    numbers = _NUMBER.findall(text)
    return float(numbers[0]) if numbers else None


def _floor(text: str, unit: str) -> str | None:
    """ "45.1MP - 59.9MP" -> "45MP+". The floor is true of the whole band."""
    low = _low(text)
    return f"{int(low)}{unit}+" if low is not None else None


def _unit(word: str) -> str:
    return _UNITS.get(word.lower(), word)


def _range(text: str) -> str:
    """ "40 inches to 49 inches" -> "40–49 inch". Shorter, and the same claim."""
    if span := _SPAN.match(text):
        low, unit_a, high, unit_b = span.groups()
        unit = _unit(unit_b or unit_a)
        return f"{low}–{high} {unit}".strip()
    if _ABOVE.search(text) and (numbers := _NUMBER.findall(text)):
        rest = _ABOVE.sub("", text).replace(numbers[0], "", 1).strip(" -")
        return (
            f"{numbers[0]} {_unit(rest)}+".replace(" +", "+")
            if rest
            else f"{numbers[0]}+"
        )
    return text


def _gigabytes(text: str) -> float | None:
    if not (found := _SIZE.search(text)):
        return None
    scale = {"tb": 1024.0, "gb": 1.0, "mb": 1 / 1024}[found.group(2).lower()]
    return float(found.group(1)) * scale


def _shown(text: str, style: str, unit: str = "") -> str | None:
    if style == FLOOR:
        return _floor(text, unit)
    if style == RANGE:
        return _range(text)
    return text


def _brand(document: dict[str, Any]) -> str | None:
    brand = _first(document.get("brand_name"))
    # "poco" beside "Samsung" on a row of buttons reads as a mistake.
    return brand.title() if brand and brand.islower() else brand


def readable_name(document: dict[str, Any]) -> str:
    """The name a customer reads. A slug ("oneplus-n6x-5g-burgundy-red-4gb-64gb",
    seen on staging) is rebuilt from the model and its option, or tidied."""
    name = str(document.get("name") or "").strip()
    if name and (" " in name or "-" not in name):
        return name
    model = str(document.get("model") or "").strip()
    if model:
        return f"{model} {str(document.get('option_value') or '').strip()}".strip()
    if name:
        return " ".join(
            part.upper() if re.fullmatch(r"\d+[a-z]{1,2}", part) else part.capitalize()
            for part in name.split("-")
            if part
        )
    return "Unnamed product"


# The features a customer may require, each 1.0 or 0.0 when the PIM sheet
# says, and simply absent when it does not - "not known" is never "no".
FEATURES = ("usb_c", "five_g", "amoled")


def phone_features(document: dict[str, Any]) -> dict[str, float]:
    found: dict[str, float] = {}
    if usb := str(_first(document.get("usb_type")) or ""):
        found["usb_c"] = 1.0 if re.search(r"type[\s-]*c", usb, re.I) else 0.0
    if network := str(_first(document.get("network_type")) or ""):
        found["five_g"] = 1.0 if "5g" in network.lower() else 0.0
    display = str(
        _first(document.get("display_type"))
        or _first(document.get("display_technology"))
        or ""
    )
    if display:
        found["amoled"] = 1.0 if re.search(r"oled", display, re.I) else 0.0
    return found


def item_from(
    shelf: str,
    document: dict[str, Any],
    price: int,
    was: int | None = None,
    stock: estate_api.Stock | None = None,
    price_confirmed: bool = False,
    pim_specs: tuple[tuple[str, str], ...] = (),
) -> Item:
    """One index document and what the estate said about it, as an `Item`."""
    pim_keys = {
        "video stabilisation": "video_stabilisation",
        "video stabilization": "video_stabilization",
        "hdmi arc": "hdmi_arc",
        "bluetooth": "bluetooth",
        "battery powered": "battery_powered",
        "ram": "ram",
        "internal storage": "internal_storage",
        "battery capacity": "battery_capacity",
        # Features a customer can require ("USB-C வேணும்"): taken from the
        # estate's PIM sheet only, never guessed from a name or a megapixel.
        "usb type": "usb_type",
        "network type": "network_type",
        "display type": "display_type",
    }
    verified = {
        pim_keys[key.casefold()]: value
        for key, value in pim_specs
        if key.casefold() in pim_keys
    }
    document = {**document, **verified}
    specs = []
    for spec in SPECS.get(shelf, ()):
        text = _first(document.get(spec.source))
        if text and (shown := _shown(text, spec.style, spec.unit)):
            specs.append((spec.label, shown))

    attributes: dict[str, str] = {}
    if brand := _brand(document):
        attributes["brand"] = brand
    if shelf == "phone":
        # Production marks keypad phones "keypad-mobile" and smartphones
        # "smartphones". A ₹1,399 keypad phone was once shown to someone who
        # shoots video on a construction site; the engine now keeps keypad
        # phones for budgets that only they fit (`engine.KEYPAD_BUDGET`).
        slugs = document.get("categories_slug") or ()
        if isinstance(slugs, list) and "keypad-mobile" in slugs:
            attributes["kind"] = "keypad"
    for name, (source, style) in ATTRIBUTES.get(shelf, {}).items():
        text = _first(document.get(source))
        if text and (shown := _shown(text, style)):
            attributes[name] = shown

    measures: dict[str, float] = {}
    for name, (source, style) in MEASURES.get(shelf, {}).items():
        text = _first(document.get(source))
        number = (
            (_gigabytes(text) if style == _GIGABYTES else _low(text)) if text else None
        )
        if number is not None:
            measures[name] = number

    # Only explicit features of this SKU qualify. MP and battery capacity
    # cannot prove stabilised video, HDMI compatibility or runtime.
    fields = {
        "phone": (
            (
                "stabilisation",
                "Video stabilisation",
                ("video_stabilization", "video_stabilisation"),
                ("yes", "true", "ois", "eis", "optical", "electronic"),
            ),
        ),
        "speaker": (
            ("hdmi_arc", "HDMI ARC", ("hdmi_arc",), ("yes", "true", "arc", "earc")),
            (
                "bluetooth",
                "Bluetooth",
                ("bluetooth", "connectivity"),
                ("yes", "true", "bluetooth"),
            ),
            (
                "portable",
                "Battery powered",
                ("battery_powered",),
                ("yes", "true", "battery powered"),
            ),
        ),
    }
    for measure, label, sources, tokens in fields.get(shelf, ()):
        for source in sources:
            value = document.get(source)
            entries = value if isinstance(value, list) else [value]
            values = [str(entry).strip() for entry in entries if entry is not None]
            if any(
                re.fullmatch(r"(?:no|false|none|not supported)", v, re.I)
                for v in values
            ):
                measures[measure] = 0.0
                break
            if any(
                re.fullmatch(
                    r"(?:" + "|".join(tokens) + r")(?: stabilization| stabilisation)?",
                    v,
                    re.I,
                )
                for v in values
            ):
                measures[measure] = 1.0
                specs.append((label, next(v for v in values if v)))
                break
    if shelf == "phone":
        measures.update(phone_features(document))
    rating = document.get("rating")
    confirmed = stock is not None and stock.state == estate_api.KNOWN
    return Item(
        item_code=str(document.get("item_code") or ""),
        checked_at=int(time.time()),
        pim_fields=tuple(sorted(verified)),
        name=readable_name(document),
        brand=brand,
        price=int(price),
        was=was if was and was > price else None,
        specs=tuple(specs),
        attributes=attributes,
        measures=measures,
        rating=float(rating)
        if isinstance(rating, (int, float)) and rating > 0
        else None,
        quantity=stock.quantity if stock and confirmed else None,
        branches=stock.branches if stock and confirmed else None,
        stock_confirmed=confirmed,
        price_confirmed=price_confirmed,
    )


def _one_of_each(items: list[tuple[dict[str, Any], Item]]) -> tuple[Item, ...]:
    """The same phone in four colours is one choice, not four of the five.

    Same model, same memory, same price: what differs is the colour, and a
    customer asked for a shortlist is not helped by the top three slots
    being one handset. The variant with the most stock stands for the rest.
    A different memory size is a different price and stays a different card.
    """
    kept: dict[tuple[Any, ...], Item] = {}
    for document, item in items:
        key = (
            document.get("model_code") or item.name,
            item.price,
            _first(document.get("ram")),
            _first(document.get("internal_storage")),
        )
        if (held := kept.get(key)) is None:
            kept[key] = item
            continue
        best = item if (item.quantity or 0) > (held.quantity or 0) else held
        kept[key] = replace(best, colours=held.colours + 1)
    return tuple(sorted(kept.values(), key=lambda item: (item.price, item.name)))


def _index_price(document: dict[str, Any]) -> int | None:
    """The index's nearest thing to a selling price. Used only with no estate."""
    for name in ("special_price", "price"):
        value = document.get(name)
        if isinstance(value, (int, float)) and value > 1:
            return int(value)
    return None


@dataclass
class _Held:
    items: tuple[Item, ...]
    at: float


class ShelfBook:
    """Every scripted shelf, as true as the estate will make it, kept warm."""

    def __init__(
        self,
        index: Index,
        estate: estate_api.Backend | None,
        ttl_s: float = TTL_SECONDS,
        wide: int = WIDE,
        patience_s: float = PATIENCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        snapshot_dir: Path | None = None,
        wall: Callable[[], float] = time.time,
        first_wait_s: float = FIRST_WAIT_SECONDS,
    ) -> None:
        self._index = index
        self._estate = estate
        self._ttl = ttl_s
        # One limit on estate calls for every shelf at once, made on first
        # use in the loop that uses it: a semaphore is bound to one event
        # loop, and a book that outlives its loop (tests, a restarted
        # lifespan) would otherwise fail every build after the first.
        self._wide = wide
        self._estate_slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        self._patience = patience_s
        self._clock = clock
        self._held: dict[str, _Held] = {}
        # The owner, 2026-11-24: "taking more time, make it faster". The
        # production phone shelf is 630 items checked one by one - about
        # three minutes. A customer never waits for that: a restart serves
        # the last built shelf from disk, and a first-ever build answers
        # the questions from the index while the estate is still asked.
        self._snapshots = snapshot_dir
        self._wall = wall
        self._first_wait = first_wait_s
        self._interim: dict[str, tuple[Item, ...]] = {}
        # The spec's four honest numbers per shelf, from the last build:
        # records the index returned, of those in stock, of those with a
        # price in force, and the distinct models they make. Never one
        # number standing in for another.
        self.counts: dict[str, dict[str, int]] = {}
        self._building: dict[str, asyncio.Task[tuple[Item, ...]]] = {}
        self._warming: asyncio.Task[None] | None = None

    async def items(self, shelf: str, verified: bool = False) -> tuple[Item, ...]:
        """The shelf, built now if nobody has asked before.

        A stale shelf is answered with what is held and rebuilt behind the
        answer: the customer in front of us gets a list that was true a
        quarter of an hour ago rather than a seven-second wait for one that
        is true now, and the product they pick is re-asked live either way.

        With nothing held, the last shelf saved to disk is served and
        rebuilt behind. With nothing saved either - the first build ever -
        the index's own shelf answers as soon as the index has, every item
        marked unconfirmed, unless `verified` is asked for: a
        recommendation waits for the estate, a question does not.
        """
        if (held := self._held.get(shelf)) is not None:
            if self._clock() - held.at > self._ttl:
                self._start(shelf)
            return held.items
        if (restored := self._restore(shelf)) is not None:
            return restored
        # Shielded: a customer who closes the tab mid-build must not cancel
        # the build for the three others waiting on the same shelf.
        task = self._start(shelf)
        if verified:
            return await asyncio.shield(task)
        waited = asyncio.get_running_loop().time() + self._first_wait
        while not task.done():
            if (
                asyncio.get_running_loop().time() >= waited
                and (interim := self._interim.get(shelf)) is not None
            ):
                return interim
            await asyncio.wait({task}, timeout=0.05)
        if task.exception() is not None and shelf in self._interim:
            # The estate failed this time; the index's shelf, unconfirmed,
            # still lets the questions run. The next ask tries again.
            return self._interim[shelf]
        return task.result()

    def _restore(self, shelf: str) -> tuple[Item, ...] | None:
        """The shelf saved by the last build, held as stale and rebuilt now."""
        if self._snapshots is None:
            return None
        path = self._snapshots / f"{shelf}.json"
        try:
            saved = json.loads(path.read_text())
            if saved.get("version") != SNAPSHOT_VERSION or saved.get("shelf") != shelf:
                return None
            if self._wall() - float(saved["built_at"]) > SNAPSHOT_MAX_AGE_SECONDS:
                return None
            items = tuple(_item_from(entry) for entry in saved["items"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as unreadable:
            log.warning("the saved %s shelf could not be read: %s", shelf, unreadable)
            return None
        # Held at its real age: a shelf saved five minutes ago is five minutes
        # old, and is rebuilt when it passes the TTL like any other. Marking
        # every restored shelf stale set off fifteen rebuilds at each restart,
        # and the customers' own card checks timed out behind them.
        age = max(0.0, self._wall() - float(saved["built_at"]))
        self._held[shelf] = _Held(items, self._clock() - age)
        if isinstance(saved.get("counts"), dict):
            self.counts[shelf] = {k: int(v) for k, v in saved["counts"].items()}
        if age > self._ttl:
            self._start(shelf)
        log.info("%s shelf: %d served from disk, %.0f s old", shelf, len(items), age)
        return items

    def _save(self, shelf: str, items: tuple[Item, ...]) -> None:
        """Written whole, then renamed: a half-written shelf is never read.

        Only a shelf the estate vouched for. The index's own shelf - what an
        outage falls back to - is never saved to be served as if checked.
        """
        if self._snapshots is None:
            return
        if not any(item.stock_confirmed or item.price_confirmed for item in items):
            return
        try:
            self._snapshots.mkdir(parents=True, exist_ok=True)
            final = self._snapshots / f"{shelf}.json"
            partial = final.with_suffix(".json.partial")
            partial.write_text(
                json.dumps(
                    {
                        "version": SNAPSHOT_VERSION,
                        "shelf": shelf,
                        "built_at": self._wall(),
                        "counts": self.counts.get(shelf, {}),
                        "items": [dataclasses.asdict(item) for item in items],
                    },
                    ensure_ascii=False,
                )
            )
            os.replace(partial, final)
        except OSError as unwritable:
            log.warning("the %s shelf could not be saved: %s", shelf, unwritable)

    def known(self, shelf: str) -> int | None:
        """How many are on this shelf, if it has been built. Never waits.

        For the first question, which must not be held up by eleven shelves
        being read: a shelf known to be empty is left off it, and one not
        yet built is offered on trust.
        """
        held = self._held.get(shelf)
        return len(held.items) if held is not None else None

    def held(self, shelf: str) -> tuple[Item, ...]:
        """What is held for this shelf, or nothing. Never waits, never builds."""
        found = self._held.get(shelf)
        return found.items if found is not None else ()

    def warm(self, shelves: Iterable[str]) -> None:
        """Start building these, one after another, without waiting.

        One at a time on purpose. Eleven shelves at once is a few hundred
        calls landing on the retailer's services in the same second, to save a
        wait that only the first customer of the day would have had.
        """
        if self._warming is not None and not self._warming.done():
            return
        wanted = [shelf for shelf in shelves if shelf not in self._held]
        if not wanted:
            return

        async def one_by_one() -> None:
            for shelf in wanted:
                try:
                    # Verified: waiting for the estate is the point. Without
                    # it every shelf was started at once - two hundred calls
                    # on a twelve-connection pool, all timing out.
                    await self.items(shelf, verified=True)
                    # A stale snapshot is immediately usable by a customer,
                    # but its background refresh still owns the warm-up slot.
                    if (refresh := self._building.get(shelf)) is not None:
                        await asyncio.shield(refresh)
                except Exception as failed:  # noqa: BLE001 - warming is best effort
                    log.warning("could not warm the %s shelf: %s", shelf, failed)

        self._warming = asyncio.ensure_future(one_by_one())

    def _start(self, shelf: str) -> asyncio.Task[tuple[Item, ...]]:
        """The build in flight for this shelf, started if there is none."""
        task = self._building.get(shelf)
        if task is None or task.done():
            task = asyncio.ensure_future(self._build_and_hold(shelf))
            self._building[shelf] = task
            task.add_done_callback(functools.partial(self._finished, shelf))
        return task

    def _finished(self, shelf: str, task: asyncio.Task[tuple[Item, ...]]) -> None:
        if self._building.get(shelf) is task:
            del self._building[shelf]
        if task.cancelled():
            return
        if (failed := task.exception()) is not None:
            log.warning("the %s shelf could not be built: %s", shelf, failed)
            if (held := self._held.get(shelf)) is not None:
                # Keep serving the old list, and leave the next attempt a
                # minute rather than one per customer.
                held.at = self._clock() - self._ttl + RETRY_SECONDS

    async def _build_and_hold(self, shelf: str) -> tuple[Item, ...]:
        started = time.perf_counter()
        items = await self._build(shelf)
        self._interim.pop(shelf, None)
        self._held[shelf] = _Held(items, self._clock())
        self._save(shelf, items)
        log.info(
            "%s shelf: %d really in stock, built in %.0f ms",
            shelf,
            len(items),
            (time.perf_counter() - started) * 1000,
        )
        return items

    async def _build(self, shelf: str) -> tuple[Item, ...]:
        documents = [d for d in await self._index.shelf(shelf) if d.get("item_code")]
        if not documents:
            self.counts[shelf] = {"records": 0, "in_stock": 0, "priced": 0, "models": 0}
            return ()
        if self._estate is not None and shelf not in self._held:
            # Nothing to serve while the estate is asked: the index's shelf,
            # every item unconfirmed, answers the questions meanwhile.
            self._interim[shelf] = self._as_indexed(shelf, documents)
        if self._estate is None:
            items = self._as_indexed(shelf, documents)
            self.counts[shelf] = {
                "records": len(documents),
                "in_stock": len(documents),
                "priced": 0,
                "models": len(items),
            }
            return items

        stocks = await self._ask(shelf, documents, (estate_api.STOCK,))
        # Kept unless we were TOLD it is gone. "Could not ask" is not "no".
        alive = [
            (document, facts.stock if facts else None)
            for document, facts in zip(documents, stocks)
            if facts is None
            or facts.stock.state == estate_api.UNKNOWN
            or facts.stock.in_stock
        ]
        sections = (
            (estate_api.PRICING, estate_api.PRODUCT)
            if shelf in {"phone", "speaker"}
            else (estate_api.PRICING,)
        )
        prices = await self._ask(shelf, [d for d, _ in alive], sections)

        if not any(stocks) and not any(prices):
            # Nothing answered at all. That is the estate being down, and
            # an empty shelf is the one thing it must not be reported as.
            log.warning("the estate answered nothing for %s; using the index", shelf)
            return self._as_indexed(shelf, documents)

        built: list[tuple[dict[str, Any], Item]] = []
        unpriced = 0
        unanswered = sum(
            1
            for facts in prices
            if facts is None or facts.price.state == estate_api.UNKNOWN
        )
        if alive and unanswered * 2 > len(alive):
            # Most prices never came back: the estate is down or swamped, and
            # an empty shelf is the one thing that must not be reported. The
            # build fails; what was held - or the index's shelf - is served.
            raise estate_api.EstateUnavailable(
                f"{unanswered} of {len(alive)} prices did not answer"
            )
        for (document, stock), facts in zip(alive, prices):
            price = facts.price if facts else None
            if price is None or price.state != estate_api.KNOWN or not price.selling:
                # No price in force means no budget it can honestly be put
                # in, and the index's figure is the one known to be wrong.
                unpriced += 1
                continue
            built.append(
                (
                    document,
                    item_from(
                        shelf,
                        document,
                        price.selling,
                        price.list_price,
                        stock,
                        True,
                        facts.specifications if facts else (),
                    ),
                )
            )
        if unpriced:
            log.info(
                "%s shelf: %d in stock but without a price in force", shelf, unpriced
            )
        items = _one_of_each(built)
        self.counts[shelf] = {
            "records": len(documents),
            "in_stock": len(alive),
            "priced": len(built),
            "models": len(items),
        }
        log.info("%s shelf: %s", shelf, self.counts[shelf])
        return items

    def _as_indexed(
        self, shelf: str, documents: list[dict[str, Any]]
    ) -> tuple[Item, ...]:
        """The shelf as the index tells it, marked as unconfirmed throughout."""
        built = [
            (document, item_from(shelf, document, price))
            for document in documents
            if (price := _index_price(document)) is not None
        ]
        return _one_of_each(built)

    async def _ask(
        self, shelf: str, documents: list[dict[str, Any]], sections: tuple[str, ...]
    ) -> list[estate_api.Facts | None]:
        """One estate question per document, a few at a time, in order."""
        estate = self._estate
        assert estate is not None  # noqa: S101 - narrowed by the caller
        loop = asyncio.get_running_loop()
        if self._estate_slots is None or self._slots_loop is not loop:
            self._estate_slots = asyncio.Semaphore(self._wide)
            self._slots_loop = loop
        slots = self._estate_slots

        async def one(document: dict[str, Any]) -> estate_api.Facts | None:
            code = str(document.get("item_code"))
            async with slots:
                try:
                    payload = await asyncio.wait_for(
                        estate.product(code, sections), self._patience
                    )
                    facts = estate_api.read(payload)
                    if facts.item_code and facts.item_code != code:
                        return None
                    return facts
                except (estate_api.EstateUnavailable, asyncio.TimeoutError) as down:
                    log.debug(
                        "estate did not answer %s for %s: %s",
                        sections,
                        code,
                        type(down).__name__,
                    )
                    return None

        answers = list(await asyncio.gather(*(one(document) for document in documents)))
        if missing := answers.count(None):
            log.warning(
                "%s shelf: %d of %d %s checks returned no usable response",
                shelf,
                missing,
                len(answers),
                "/".join(sections),
            )
        return answers


_ITEM_FIELDS = {f.name for f in dataclasses.fields(Item)}


def _item_from(entry: dict[str, Any]) -> Item:
    """One saved item back, tuples where the dataclass keeps tuples."""
    kept = {k: v for k, v in entry.items() if k in _ITEM_FIELDS}
    kept["specs"] = tuple(tuple(pair) for pair in kept.get("specs") or ())
    kept["pim_fields"] = tuple(kept.get("pim_fields") or ())
    return Item(**kept)
