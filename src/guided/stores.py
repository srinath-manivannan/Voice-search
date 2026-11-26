"""A verified shop, handed off by the record the inventory named.

Directions use the store's coordinates. A free-text search built from a
brand and a branch name is how a shopper is sent to the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass


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
