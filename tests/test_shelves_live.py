"""Every shelf the agent may filter on, against the live catalogue.

    CATALOGUE_TOKEN=... .venv/bin/python -m pytest -m live tests/test_shelves_live.py

Free - the catalogue is the retailer's own - but it needs the network and the
token, so it is excluded from the default run and CI never sees it.

This is the test that was missing. `tv` mapped to "Televisions", the
catalogue's shelf is "Television", and the filter therefore matched nothing
at all: the one category a customer asks for by name as often as phones
returned an empty shortlist on every call, and no unit test could see it
because the value is only wrong against data nobody had checked.

A facet value is a string agreed with a system we do not own. The only thing
that can tell us it is still right is that system.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from src.agent.tools.catalogue import CATEGORIES, Catalogue

pytestmark = pytest.mark.live


def catalogue() -> Catalogue:
    load_dotenv()
    token = os.environ.get("CATALOGUE_TOKEN", "")
    if not token:
        pytest.skip("CATALOGUE_TOKEN not set")
    return Catalogue(
        os.environ.get("CATALOGUE_URL", ""),
        token,
        origin=os.environ.get("CATALOGUE_ORIGIN", "bot"),
    )


@pytest.mark.parametrize("spoken", sorted(set(CATEGORIES)))
async def test_the_shelf_exists_and_has_something_on_it(spoken: str):
    """A mapping that finds nothing is worse than no mapping at all.

    With no mapping the query still searches the whole catalogue. With a
    wrong one the filter excludes everything, and the agent tells a customer
    the retailer does not sell televisions.
    """
    found = await catalogue().search(query="*", category=spoken, limit=1)

    assert found.total > 0, (
        f"category '{spoken}' maps to the facet {CATEGORIES[spoken]!r}, "
        f"which matches nothing in the live catalogue"
    )


async def test_the_catalogue_reaches_past_the_shelves_we_name():
    """The enum is a filter, not the inventory.

    The retailer sells a great deal that has no entry in CATEGORIES, and the
    tool's description tells the model to use the query for those. If that
    stopped working the agent would be back to answering about five shelves.
    """
    shop = catalogue()
    for words in ("mixer grinder", "trimmer", "bluetooth speaker"):
        found = await shop.search(query=words, limit=2)
        assert found.total > 0, f"nothing found for {words!r} by text alone"
