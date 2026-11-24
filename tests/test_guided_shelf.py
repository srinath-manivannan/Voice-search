"""The shelf a guided call narrows must be true before the first question.

Every test here is one way the search index was wrong on 2026-11-23 - it
called 70 phones in stock when 16 were, and priced one at 14,499 that sells
for 20,999 - or one way the estate can fail to answer, which must never be
read as an empty shelf.
"""

from __future__ import annotations

import asyncio

from src.agent.tools import estate as estate_api
from src.guided.shelf import ShelfBook, item_from
from tests.guided_fakes import FakeEstate, FakeIndex, indexed, on_shelf


def book(documents: list[dict], estate: FakeEstate | None, **kwargs) -> ShelfBook:
    return ShelfBook(FakeIndex({"phone": documents}), estate, **kwargs)


async def test_what_the_index_calls_in_stock_and_the_shop_does_not_is_left_out():
    documents = [indexed("1", "Galaxy A17", 25_499), indexed("2", "iPhone 12", 49_990)]
    estate = FakeEstate(
        {"1": on_shelf("1", 38_499), "2": on_shelf("2", 49_990, in_stock=False)}
    )

    items = await book(documents, estate).items("phone")

    assert [item.item_code for item in items] == ["1"]


async def test_the_price_shown_is_the_price_in_force_and_never_the_index_figure():
    documents = [indexed("1", "Realme P4 Lite", 14_499)]
    estate = FakeEstate({"1": on_shelf("1", 20_999, mrp=22_999)})

    (item,) = await book(documents, estate).items("phone")

    assert (item.price, item.was) == (20_999, 22_999)
    assert item.price_confirmed and item.stock_confirmed


async def test_stock_is_asked_first_and_only_what_is_there_is_priced():
    """54 of 70 phones never needed a price. Asking for it anyway was 54 calls."""
    documents = [indexed(str(n), f"Phone {n}", 20_000) for n in range(1, 5)]
    estate = FakeEstate(
        {str(n): on_shelf(str(n), 20_000, in_stock=(n == 1)) for n in range(1, 5)}
    )

    await book(documents, estate).items("phone")

    priced = [code for code, sections in estate.asked if estate_api.PRICING in sections]
    assert priced == ["1"]
    assert len(estate.asked) == 5, "four stock checks and one price"


async def test_stock_we_could_not_ask_about_is_kept_and_marked_unconfirmed():
    """Could not ask is not no. The item stays, and the card says so."""
    documents = [indexed("1", "Galaxy A17", 25_499)]
    unknown = on_shelf("1", 38_499)
    unknown["sections"]["stock"] = {"status": "error", "reason": "signature is invalid"}

    (item,) = await book(documents, FakeEstate({"1": unknown})).items("phone")

    assert item.price == 38_499 and item.stock_confirmed is False
    assert item.quantity is None and item.branches is None


async def test_in_stock_with_no_price_in_force_cannot_be_put_in_a_budget():
    documents = [indexed("1", "Galaxy A17", 25_499)]
    no_price = on_shelf("1", 0)
    no_price["sections"]["pricing"] = {"status": "empty"}

    assert await book(documents, FakeEstate({"1": no_price})).items("phone") == ()


async def test_an_estate_that_answers_nothing_is_an_outage_and_not_an_empty_shelf():
    documents = [indexed("1", "Galaxy A17", 25_499), indexed("2", "Galaxy A56", 43_999)]

    items = await book(documents, FakeEstate({})).items("phone")

    assert [item.price for item in items] == [25_499, 43_999]
    assert not any(item.price_confirmed or item.stock_confirmed for item in items)


async def test_with_no_estate_configured_the_shelf_is_the_index_marked_as_such():
    (item,) = await book([indexed("1", "Galaxy A17", 25_499)], None).items("phone")

    assert item.price == 25_499 and not item.price_confirmed


async def test_one_phone_in_three_colours_is_one_choice():
    documents = [
        indexed("1", "Nokia 105 ( Charcoal )", 1_549, model_code="nokia-105"),
        indexed("2", "Nokia 105 ( Cyan )", 1_549, model_code="nokia-105"),
        indexed("3", "Nokia 105 ( Red )", 1_549, model_code="nokia-105"),
        indexed("4", "Nokia 105 4G", 2_299, model_code="nokia-105"),
    ]
    estate = FakeEstate(
        {
            "1": on_shelf("1", 1_549, quantity=2),
            "2": on_shelf("2", 1_549, quantity=9),
            "3": on_shelf("3", 1_549, quantity=4),
            "4": on_shelf("4", 2_299),
        }
    )

    items = await book(documents, estate).items("phone")

    assert [(item.item_code, item.colours) for item in items] == [("2", 3), ("4", 1)], (
        "the best-stocked colour stands for the model; a different price is a different card"
    )


async def test_a_shelf_is_built_once_however_many_are_waiting_for_it():
    index = FakeIndex({"phone": [indexed("1", "Galaxy A17", 25_499)]})
    shelves = ShelfBook(index, FakeEstate({"1": on_shelf("1", 38_499)}))

    first, second, third = await asyncio.gather(
        shelves.items("phone"), shelves.items("phone"), shelves.items("phone")
    )

    assert first == second == third and index.reads == ["phone"]
    await shelves.items("phone")
    assert index.reads == ["phone"], "a held shelf was read again"


async def test_a_stale_shelf_is_answered_at_once_and_rebuilt_behind_the_answer():
    now = [0.0]
    index = FakeIndex({"phone": [indexed("1", "Galaxy A17", 25_499)]})
    estate = FakeEstate({"1": on_shelf("1", 38_499)})
    shelves = ShelfBook(index, estate, ttl_s=900, clock=lambda: now[0])
    await shelves.items("phone")

    now[0] = 901.0
    estate.by_code["1"] = on_shelf("1", 36_999)
    (stale,) = await shelves.items("phone")
    assert stale.price == 38_499, "the customer was made to wait for the rebuild"

    await asyncio.sleep(0.01)
    (fresh,) = await shelves.items("phone")
    assert fresh.price == 36_999 and index.reads == ["phone", "phone"]


async def test_a_rebuild_that_fails_leaves_the_old_shelf_and_does_not_retry_per_customer():
    now = [0.0]
    index = FakeIndex({"phone": [indexed("1", "Galaxy A17", 25_499)]})
    shelves = ShelfBook(
        index, FakeEstate({"1": on_shelf("1", 38_499)}), ttl_s=900, clock=lambda: now[0]
    )
    await shelves.items("phone")

    now[0], index.down = 901.0, True
    for _ in range(5):
        assert len(await shelves.items("phone")) == 1
        await asyncio.sleep(0.01)

    assert index.reads == ["phone", "phone"], "every customer started their own rebuild"


async def test_a_cold_shelf_that_cannot_be_read_says_so():
    import pytest

    from src.agent.tools.catalogue import CatalogueUnavailable

    shelves = ShelfBook(FakeIndex({}, down=True), None)

    with pytest.raises(CatalogueUnavailable):
        await shelves.items("phone")
    assert shelves.known("phone") is None


def test_a_band_with_no_floor_is_not_printed_as_one():
    """ "Less than 2499 mAh" has a number in it. It is a ceiling."""
    document = indexed(
        "1",
        "Nokia 105",
        1_549,
        battery_capacity=["Less than 2499 mAh"],
        primary_camera=["45.1MP - 59.9MP"],
        internal_storage=["1TB"],
        ram=["8GB"],
    )

    item = item_from("phone", document, 1_549)

    assert ("கேமரா", "45MP+") in item.specs
    assert not any(label == "பேட்டரி" for label, _ in item.specs)
    assert item.measures == {"camera_mp": 45.1, "ram_gb": 8.0, "storage_gb": 1024.0}


def test_ranges_are_shortened_without_changing_what_they_claim():
    tv = item_from(
        "tv", indexed("1", "TV", 1, screen_size=["40 inches to 49 inches"]), 1
    )
    ac = item_from("ac", indexed("2", "AC", 1, capacity=["2 Ton & Above"]), 1)
    fridge = item_from("fridge", indexed("3", "F", 1, capacity=["201 L - 250 L"]), 1)

    assert tv.attributes["size"] == "40–49 inch"
    assert ac.attributes["capacity"] == "2 Ton+"
    assert ("கெபாசிட்டி", "201–250 L") in fridge.specs


def test_a_lowercase_brand_is_capitalised_and_a_was_price_must_be_higher():
    item = item_from(
        "phone", indexed("1", "Poco M8", 1, brand="poco"), 24_999, was=24_999
    )

    assert item.brand == "Poco" and item.attributes["brand"] == "Poco"
    assert item.was is None, "a 'was' equal to the price is not a discount"


async def test_the_four_counts_are_kept_apart():
    """Records, in stock, priced, models: the spec forbids one number
    standing in for another - 12,934 index records are not 12,934 phones."""
    documents = [
        indexed("1", "Nokia 105 ( Charcoal )", 1_549, model_code="nokia-105"),
        indexed("2", "Nokia 105 ( Cyan )", 1_549, model_code="nokia-105"),
        indexed("3", "Galaxy A17", 25_499),
        indexed("4", "iPhone 12", 49_990),
        indexed("5", "Redmi 13", 11_999),
    ]
    no_price = on_shelf("5", 0)
    no_price["sections"]["pricing"] = {"status": "empty"}
    estate = FakeEstate(
        {
            "1": on_shelf("1", 1_549),
            "2": on_shelf("2", 1_549),
            "3": on_shelf("3", 38_499),
            "4": on_shelf("4", 49_990, in_stock=False),
            "5": no_price,
        }
    )
    shelves = book(documents, estate)

    items = await shelves.items("phone")

    assert len(items) == 2
    assert shelves.counts["phone"] == {
        "records": 5,
        "in_stock": 4,
        "priced": 3,
        "models": 2,
    }


# ── speed: nobody waits for a build (owner, 2026-11-24) ─────────────────────


class SlowEstate(FakeEstate):
    """An estate that takes its time, like 630 production phones do."""

    def __init__(self, by_code: dict[str, dict], delay: float) -> None:
        super().__init__(by_code)
        self.delay = delay

    async def product(self, item_code: str, sections: tuple[str, ...]) -> dict:
        await asyncio.sleep(self.delay)
        return await super().product(item_code, sections)


def _two_phones():
    documents = [indexed("1", "Galaxy A17", 25_499), indexed("2", "Redmi 13", 11_999)]
    estate = {"1": on_shelf("1", 24_999), "2": on_shelf("2", 11_499)}
    return documents, estate


async def test_a_restart_serves_the_saved_shelf_at_once_and_rebuilds_behind(tmp_path):
    documents, answers = _two_phones()
    first = book(documents, FakeEstate(answers), snapshot_dir=tmp_path)
    built = await first.items("phone")

    index = FakeIndex({"phone": documents})
    restarted = ShelfBook(index, SlowEstate(answers, delay=5.0), snapshot_dir=tmp_path)
    started = asyncio.get_running_loop().time()
    served = await restarted.items("phone")

    assert asyncio.get_running_loop().time() - started < 0.5, "no wait for the estate"
    assert served == built and all(i.stock_confirmed for i in served)
    assert restarted.counts["phone"]["models"] == 2
    await asyncio.sleep(0)
    assert index.reads == [], "just saved: no rebuild until it is old"


async def test_a_saved_shelf_past_its_ttl_is_served_and_rebuilt_behind(tmp_path):
    documents, answers = _two_phones()
    now = [1_000_000.0]
    await book(
        documents, FakeEstate(answers), snapshot_dir=tmp_path, wall=lambda: now[0]
    ).items("phone")
    now[0] += 20 * 60  # twenty minutes later; the TTL is fifteen

    index = FakeIndex({"phone": documents})
    restarted = ShelfBook(
        index,
        SlowEstate(answers, delay=5.0),
        snapshot_dir=tmp_path,
        wall=lambda: now[0],
    )
    served = await restarted.items("phone")
    await asyncio.sleep(0)

    assert {i.item_code for i in served} == {"1", "2"}
    assert index.reads == ["phone"], "the rebuild is already under way"


async def test_a_saved_shelf_older_than_six_hours_is_not_shown(tmp_path):
    documents, answers = _two_phones()
    now = [1_000_000.0]
    await book(
        documents, FakeEstate(answers), snapshot_dir=tmp_path, wall=lambda: now[0]
    ).items("phone")
    now[0] += 6 * 3600 + 1

    index = FakeIndex({"phone": []})
    later = ShelfBook(index, FakeEstate({}), snapshot_dir=tmp_path, wall=lambda: now[0])

    assert await later.items("phone") == (), "built fresh, not from the old file"


async def test_a_damaged_saved_shelf_is_ignored(tmp_path):
    (tmp_path / "phone.json").write_text("{not json")
    documents, answers = _two_phones()

    items = await book(documents, FakeEstate(answers), snapshot_dir=tmp_path).items(
        "phone"
    )

    assert {i.item_code for i in items} == {"1", "2"}


async def test_a_first_build_answers_questions_from_the_index_meanwhile():
    """No snapshot and a slow estate: the questions get the index's shelf,
    marked unconfirmed, at once; a recommendation waits for the estate."""
    documents, answers = _two_phones()
    shelves = book(documents, SlowEstate(answers, delay=0.3), first_wait_s=0.05)

    quick = await shelves.items("phone")
    assert {i.item_code for i in quick} == {"1", "2"}
    assert not any(i.stock_confirmed or i.price_confirmed for i in quick)

    verified = await shelves.items("phone", verified=True)
    assert all(i.stock_confirmed and i.price_confirmed for i in verified)
    assert {i.price for i in verified} == {24_999, 11_499}, "the estate's prices"


class PricesTimeOut(FakeEstate):
    """Stock answers; prices do not - the estate swamped, not empty."""

    async def product(self, item_code: str, sections: tuple[str, ...]) -> dict:
        if estate_api.PRICING in sections:
            raise estate_api.EstateUnavailable("timed out")
        return await super().product(item_code, sections)


async def test_prices_that_never_answered_are_an_outage_not_an_empty_shelf(tmp_path):
    """Live, 2026-11-24: seventeen shelves built at once timed out and were
    saved EMPTY - speaker had 76 in stock and 0 priced. Now the build fails,
    nothing is saved, and the index's shelf keeps the questions running."""
    documents, answers = _two_phones()
    shelves = book(
        documents, PricesTimeOut(answers), snapshot_dir=tmp_path, first_wait_s=0.05
    )

    served = await shelves.items("phone")

    assert {i.item_code for i in served} == {"1", "2"}
    assert not any(i.price_confirmed for i in served), "the index's, unconfirmed"
    assert not (tmp_path / "phone.json").exists(), "an outage is never saved"


async def test_warming_builds_one_shelf_at_a_time():
    """The same live failure from the other side: the warm loop must wait for
    each verified build before starting the next."""
    documents, answers = _two_phones()
    estate = SlowEstate(answers, delay=0.05)
    index = FakeIndex({"phone": documents, "speaker": documents})
    shelves = ShelfBook(index, estate)

    shelves.warm(("phone", "speaker"))
    await asyncio.sleep(0.02)

    assert index.reads == ["phone"], "speaker waits for phone to be verified"
    await shelves._warming
    assert index.reads == ["phone", "speaker"]


async def test_a_quick_build_is_waited_for_so_no_unchecked_price_is_shown():
    """The estate answering in milliseconds must never lose the race to the
    index's own shelf - it carries list prices, not the prices in force."""
    documents, answers = _two_phones()

    items = await book(documents, FakeEstate(answers)).items("phone")

    assert all(i.price_confirmed for i in items)
    assert {i.price for i in items} == {24_999, 11_499}


def test_a_slug_for_a_name_is_shown_as_a_name():
    from src.guided.shelf import readable_name

    assert readable_name({"name": "oneplus-n6x-5g-burgundy-red-4gb-64gb"}) == (
        "Oneplus N6x 5G Burgundy Red 4GB 64GB"
    )
    assert (
        readable_name(
            {
                "name": "oneplus-n6x",
                "model": "OnePlus N6x",
                "option_value": "( Red, 4GB )",
            }
        )
        == "OnePlus N6x ( Red, 4GB )"
    )
    assert readable_name({"name": "Galaxy A17 ( Black )"}) == "Galaxy A17 ( Black )"
