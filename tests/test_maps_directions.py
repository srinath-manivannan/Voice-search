"""Maps links for a branch: coordinates first, then a deduped address."""

from __future__ import annotations

from urllib.parse import unquote

from src.config import load
from src.guided.stores import directions_url


def test_coordinates_win_over_a_duplicate_city_and_a_maps_link():
    url = directions_url(
        {
            "name": "Kanchipuram",
            "city": "Kanchipuram",
            "lat": 12.83,
            "lng": 79.7,
            "maps_url": "https://www.google.com/maps/place/example",
        },
        prefix="My Store",
    )
    assert url == "https://www.google.com/maps/search/?api=1&query=12.83,79.7"
    assert "Kanchipuram" not in url


def test_latitude_and_longitude_are_accepted():
    url = directions_url({"latitude": "12.834", "longitude": "79.703", "city": "X"})
    assert url.endswith("query=12.834,79.703")


def test_a_direct_maps_url_is_used_when_there_are_no_coordinates():
    link = "https://www.google.com/maps/place/branch-12"
    assert directions_url({"name": "Kanchipuram", "maps_url": link}) == link


def test_prefix_is_prepended_and_the_city_is_not_repeated():
    url = directions_url(
        {
            "name": "Kanchipuram",
            "city": "kanchipuram",
            "address": "12 Temple Street",
            "state": "Tamil Nadu",
            "pincode": "631501",
        },
        prefix="My Store",
    )
    query = unquote(url.split("query=", 1)[1])
    assert query == "My Store Kanchipuram 12 Temple Street Tamil Nadu 631501"
    assert query.casefold().count("kanchipuram") == 1


def test_the_generic_assistant_name_is_not_added_to_a_maps_query(monkeypatch):
    monkeypatch.delenv("STORE_MAP_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("BRAND_NAME", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    made = load("development")
    assert made.store_map_query_prefix == ""
    url = directions_url(
        {"branch": "Kanchipuram", "city": "Kanchipuram"},
        prefix=made.store_map_query_prefix,
    )
    assert "Shopping Assistant" not in url
    assert unquote(url.split("query=", 1)[1]) == "Kanchipuram"


def test_a_custom_brand_is_the_prefix_only_when_no_override_is_set(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("BRAND_NAME", "Northwind")
    monkeypatch.delenv("STORE_MAP_QUERY_PREFIX", raising=False)
    assert load("development").store_map_query_prefix == "Northwind"
    monkeypatch.setenv("STORE_MAP_QUERY_PREFIX", "My Store")
    assert load("development").store_map_query_prefix == "My Store"
