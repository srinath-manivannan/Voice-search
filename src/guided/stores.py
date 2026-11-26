"""A verified shop, handed off by the record the inventory named.

Directions use the store's coordinates. A free-text search built from a
brand and a branch name is how a shopper is sent to the wrong place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class StoreRecord:
    store_id: str
    name: str
    address: str
    lat: float
    lng: float
    hours: str
    verified_at: str
    phone: str = ""


def handoff(
    store: StoreRecord, *, distance_km: float | None = None
) -> dict[str, object]:
    """What the screen and the host may use. No search string, no brand."""
    return {
        "store_id": store.store_id,
        "name": store.name,
        "address": store.address,
        "geo": {"lat": store.lat, "lng": store.lng},
        "hours": store.hours,
        "verified_at": store.verified_at,
        "phone": store.phone,
        "distance_km": distance_km,
        "directions": f"{store.lat},{store.lng}",
    }


_MAPS = "https://www.google.com/maps/search/?api=1&query="
_TOKEN = re.compile(r"[^\w]+", re.UNICODE)


def directions_url(branch: Mapping[str, Any], *, prefix: str = "") -> str:
    """A maps link for one branch.

    Coordinates win. A direct maps link is next. Otherwise the search is the
    address, with the branch name and the city written once each. The generic
    assistant name is never added; ``prefix`` is the only extra wording.
    """
    coords = _coordinates(branch)
    if coords is not None:
        lat, lng = coords
        return _MAPS + quote(f"{_coord(lat)},{_coord(lng)}", safe=",")
    direct = _direct_maps(branch)
    if direct:
        return direct
    query = _place_query(branch, prefix)
    if not query:
        return ""
    return _MAPS + quote(query, safe=",")


def _coordinates(branch: Mapping[str, Any]) -> tuple[float, float] | None:
    geo = branch.get("geo")
    source: Mapping[str, Any] = geo if isinstance(geo, Mapping) else branch
    lat = _number(source.get("lat"))
    if lat is None:
        lat = _number(source.get("latitude"))
    lng = _number(source.get("lng"))
    if lng is None:
        lng = _number(source.get("longitude"))
    if lat is None or lng is None:
        return None
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
        return None
    return lat, lng


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _coord(number: float) -> str:
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def _direct_maps(branch: Mapping[str, Any]) -> str:
    for key in ("maps_url", "map_url", "maps", "directions_url"):
        found = _http_maps(branch.get(key))
        if found:
            return found
    return ""


def _http_maps(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text.startswith(("https://", "http://")):
        return ""
    if "maps" not in text.casefold():
        return ""
    return text


def _place_query(branch: Mapping[str, Any], prefix: str) -> str:
    name = str(branch.get("name") or branch.get("label") or branch.get("branch") or "")
    parts = (
        prefix,
        name,
        str(branch.get("address") or ""),
        str(branch.get("area") or ""),
        str(branch.get("city") or ""),
        str(branch.get("state") or ""),
        str(branch.get("pincode") or branch.get("pin") or ""),
    )
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        for token in part.split():
            key = _TOKEN.sub("", token).casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(token)
    return " ".join(kept)
