"""The question engine: a salesperson's order, and what is refused.

All of it pure, so all of it free to test exhaustively - no shelf is read
and no model is called anywhere in this file.
"""

from __future__ import annotations

import pytest

from src.guided import engine
from src.guided.engine import Answer, Ask, NotOnOffer, Ready
from src.guided.script import (
    ANY,
    BRAND,
    BUDGET,
    BY_SHELF,
    ENGLISH,
    PRIORITY,
    PURPOSE,
    USE,
    rupees,
)
from tests.guided_fakes import phone

PHONES = BY_SHELF["phone"]
# One brand, so the brand question never fires and tests can reach the
# later questions without wading through it.
ONE_BRAND = dict(brand="Samsung")


def shelf_of(*prices: int, **each) -> tuple:
    return tuple(
        phone(str(n), price, **each) for n, price in enumerate(prices, start=1)
    )


def walk(items, *taps, lang="ta"):
    """Tap through the questions as the page would, validating every tap."""
    answers: tuple = ()
    for question, choice in taps:
        answers = engine.answer(PHONES, items, answers, question, choice, lang)
    return answers


# Seven phones across three budget bands, one brand: the brand question
# cannot split them, the budget question can.
SEVEN = shelf_of(8_000, 12_000, 18_000, 22_000, 26_000, 34_000, 52_000, **ONE_BRAND)


def test_a_lakh_is_grouped_the_way_a_price_tag_groups_it():
    assert rupees(999) == "₹999"
    assert rupees(15_000) == "₹15,000"
    assert rupees(125_000) == "₹1,25,000"
    assert rupees(12_345_678) == "₹1,23,45,678"


def test_the_ladder_is_open_at_both_ends_and_has_no_gap_or_overlap():
    ladder = engine.bands(PHONES)

    assert ladder[0].low is None and ladder[-1].high is None
    for price in (1, 9_999, 10_000, 14_999, 15_000, 49_999, 50_000, 900_000):
        assert sum(band.holds(price) for band in ladder) == 1, price
    assert ladder[0].label == "₹10,000-க்குள்"
    assert ladder[-1].label == "₹50,000-க்கு மேல்"
    english = engine.bands(PHONES, ENGLISH)
    assert english[0].label == "Under ₹10,000"
    assert english[-1].label == "Over ₹50,000"


def test_the_questions_continue_however_few_remain():
    """The consultation is never skipped because stock is thin. The owner
    walked into the old shortcut on the speaker shelf: brand tapped,
    products thrown at him. Four items still get the whole ladder."""
    items = shelf_of(8_000, 18_000, 25_000, 40_000)

    step = engine.next_step(PHONES, items, ())
    assert isinstance(step, Ask) and step.question == BUDGET

    answers = walk(
        items,
        (BUDGET, ANY),
        (PURPOSE, "family"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    )
    step = engine.next_step(PHONES, items, answers)
    assert isinstance(step, Ready) and len(step.items) == 4


def test_an_empty_shelf_is_ready_with_nothing_rather_than_a_question():
    step = engine.next_step(PHONES, (), ())

    assert isinstance(step, Ready) and step.items == ()


def test_brand_is_asked_before_budget_the_way_a_salesperson_asks():
    """The customer who rehearsed this call said brand first, then budget."""
    items = (
        *(phone(f"s{n}", 11_000 + n * 7_000, brand="Samsung") for n in range(3)),
        *(phone(f"v{n}", 22_000 + n * 9_000, brand="Vivo") for n in range(2)),
        phone("o1", 43_000, brand="Oppo"),
    )

    step = engine.next_step(PHONES, items, ())

    assert isinstance(step, Ask) and step.question == BRAND
    assert [(c.id, c.count) for c in step.choices] == [
        ("Samsung", 3),
        ("Vivo", 2),
        ("Oppo", 1),
        (ANY, 6),
    ]
    after = engine.next_step(PHONES, items, walk(items, (BRAND, ANY)))
    assert isinstance(after, Ask) and after.question == BUDGET


def test_a_question_that_cannot_split_the_list_is_not_asked():
    """Seven phones, one brand: "which brand?" has one answer."""
    step = engine.next_step(PHONES, SEVEN, ())

    assert isinstance(step, Ask) and step.question == BUDGET, "brand was asked anyway"


def test_only_bands_with_something_in_them_are_drawn():
    items = shelf_of(8_000, 9_000, 22_000, 24_000, 26_000, 28_000, 60_000, **ONE_BRAND)

    step = engine.next_step(PHONES, items, ())

    assert isinstance(step, Ask) and step.question == BUDGET
    drawn = {choice.id: choice.count for choice in step.choices}
    assert drawn == {"0-10000": 2, "20000-30000": 4, "50000-": 1, ANY: 7}
    assert "10000-15000" not in drawn, "an empty band was offered"


def test_the_full_ladder_ends_in_exactly_what_was_asked_for():
    items = shelf_of(8_000, 9_000, 22_000, 24_000, 26_000, 28_000, 60_000, **ONE_BRAND)

    answers = walk(
        items,
        (BUDGET, "20000-30000"),
        (PURPOSE, "family"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    )
    step = engine.next_step(PHONES, items, answers)

    assert isinstance(step, Ready)
    assert [item.price for item in step.items] == [22_000, 24_000, 26_000, 28_000]


def test_purpose_is_asked_after_budget_and_a_student_is_asked_what_they_study():
    answers = walk(SEVEN, (BUDGET, ANY))

    step = engine.next_step(PHONES, SEVEN, answers)
    assert isinstance(step, Ask) and step.question == PURPOSE
    assert [c.id for c in step.choices] == ["study", "work", "fun", "family"]

    answers = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "study"))
    step = engine.next_step(PHONES, SEVEN, answers)
    assert isinstance(step, Ask) and step.question == USE
    assert step.ask == "என்ன படிக்கறீங்க?"
    assert [c.id for c in step.choices] == ["school", "college", "course"]


def test_the_tree_goes_as_deep_as_the_owner_scripted_it():
    """Professional -> what work -> what kind of IT work -> what matters,
    asked plainly: a job title is not a spec (owner's brief, 2026-11-24)."""
    working = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "work"))
    step = engine.next_step(PHONES, SEVEN, working)
    assert isinstance(step, Ask) and step.question == USE
    assert step.ask == "என்ன மாதிரி வேலை?"

    it_work = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "work"), (USE, "it"))
    step = engine.next_step(PHONES, SEVEN, it_work)
    assert isinstance(step, Ask) and step.question == "use2"
    assert step.ask == "IT-ல என்ன மாதிரி வேலை?"
    assert [c.id for c in step.choices] == ["dev", "meetings", "light"]

    developer = walk(
        SEVEN, (BUDGET, ANY), (PURPOSE, "work"), (USE, "it"), ("use2", "dev")
    )
    step = engine.next_step(PHONES, SEVEN, developer)
    assert isinstance(step, Ask) and step.question == PRIORITY
    assert step.ask == "உங்களுக்கு எது முக்கியம்?"
    assert step.note == "", "no '8GB RAM' presumed for a developer"
    assert "சரி, அப்படியே பாரு" not in {c.label for c in step.choices}


def test_a_leaf_purpose_needs_no_follow_up():
    family = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "family"))
    step = engine.next_step(PHONES, SEVEN, family)
    assert isinstance(step, Ask) and step.question == PRIORITY, (
        "family has no follow-up"
    )


def test_the_suggestion_is_put_as_a_question_the_customer_may_overrule():
    """What the customer DOES may suggest a spec: photos and reels suggest
    the camera - and they may still say battery."""
    answers = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "fun"), (USE, "camera"))

    step = engine.next_step(PHONES, SEVEN, answers)

    assert isinstance(step, Ask) and step.question == PRIORITY
    assert step.note == "நல்ல கேமரா முதல்ல"
    assert step.choices[0].id == "camera", "the suggestion comes first"
    assert step.choices[0].label == "சரி, அப்படியே பாரு"
    assert {c.id for c in step.choices[1:]} == {
        "performance",
        "battery",
        "storage",
        "value",
        "steady",
    }

    agreed = engine.next_step(
        PHONES,
        SEVEN,
        (
            *answers,
            Answer(PRIORITY, "camera", "x"),
            Answer(engine.CONFIRM, "yes", "x"),
        ),
    )
    assert isinstance(agreed, Ready) and agreed.priority == "camera"
    overruled = engine.next_step(
        PHONES,
        SEVEN,
        (
            *answers,
            Answer(PRIORITY, "battery", "x"),
            Answer(engine.CONFIRM, "yes", "x"),
        ),
    )
    assert isinstance(overruled, Ready) and overruled.priority == "battery"


def test_purpose_and_its_follow_up_order_the_answer_and_remove_nothing():
    answers = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "work"), (USE, "field"))

    assert len(engine.matching(PHONES, SEVEN, answers)) == len(SEVEN)


def test_the_speaker_shelf_asks_its_own_priorities_and_takes_the_answer():
    """Speakers grew their own priorities, so the spec question is asked
    there too - and the chosen one orders the result."""
    speakers = BY_SHELF["speaker"]
    items = tuple(phone(f"s{n}", 3_000 + n * 900, brand="JBL") for n in range(1, 7))
    answers = ()
    for question, choice in (
        (BUDGET, ANY),
        (PURPOSE, "party"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    ):
        answers = engine.answer(speakers, items, answers, question, choice)

    step = engine.next_step(speakers, items, answers)

    assert isinstance(step, Ready) and step.priority == "value"


def test_just_over_the_budget_is_a_question_before_it_is_ever_shown():
    """The owner's script: "the right one is a little over - shall I show
    it?" Nothing above the budget appears until the customer says yes."""
    items = (
        *shelf_of(8_000, 22_000, 24_000, **ONE_BRAND),
        phone("x1", 31_000, brand="Samsung"),  # 3.3% over 30,000
        phone("x2", 34_000, brand="Samsung"),  # 13% over
        phone("x3", 36_000, brand="Samsung"),  # 20% over - not "a little"
    )
    ladder = (
        (BUDGET, "20000-30000"),
        (PURPOSE, "family"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    )
    answers = walk(items, *ladder)

    step = engine.next_step(PHONES, items, answers)
    assert isinstance(step, Ask) and step.question == engine.BUDGET_UP
    assert [(c.id, c.count) for c in step.choices] == [("yes", 2), ("no", 2)]

    agreed = walk(items, *ladder, (engine.BUDGET_UP, "yes"))
    step = engine.next_step(PHONES, items, agreed)
    assert isinstance(step, Ready)
    assert [i.price for i in step.items] == [22_000, 24_000]
    assert [i.price for i in step.stretch] == [31_000, 34_000]

    declined = walk(items, *ladder, (engine.BUDGET_UP, "no"))
    step = engine.next_step(PHONES, items, declined)
    assert isinstance(step, Ready) and step.stretch == ()


def test_any_budget_has_nothing_to_be_over():
    answers = walk(
        SEVEN,
        (BUDGET, ANY),
        (PURPOSE, "family"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    )

    step = engine.next_step(PHONES, SEVEN[:4], answers)

    assert isinstance(step, Ready) and step.stretch == ()


def test_a_full_screen_is_not_padded_from_above_and_the_question_is_not_asked():
    items = shelf_of(12_000, 21_000, 22_000, 23_000, 24_000, 31_000, **ONE_BRAND)

    answers = walk(
        items,
        (BUDGET, "20000-30000"),
        (PURPOSE, "family"),
        (PRIORITY, "value"),
        (engine.CONFIRM, "yes"),
    )
    step = engine.next_step(PHONES, items, answers)

    assert isinstance(step, Ready) and step.stretch == ()


def test_an_answer_to_a_question_that_is_not_being_asked_is_refused():
    with pytest.raises(NotOnOffer):
        engine.answer(PHONES, SEVEN, (), PRIORITY, "camera")


@pytest.mark.parametrize(
    "forged",
    ["1-99999999", "10000-15000", "brand_name:=`x` || price:>0", ""],
)
def test_an_answer_that_is_not_one_of_the_buttons_is_refused(forged: str):
    """Nothing a client sends becomes a filter. It names a button or it is
    refused. 10000-15000 is a real band of this ladder - with nothing in
    it, so it was never drawn, so it cannot be answered. ("ANY" by name is
    no longer here: spoken ids arrive in any case and fold onto the real
    button, which is the point of the case fold.)"""
    items = shelf_of(8_000, 9_000, 22_000, 24_000, 26_000, 28_000, 60_000, **ONE_BRAND)

    with pytest.raises(NotOnOffer):
        engine.answer(PHONES, items, (), BUDGET, forged)


def test_back_takes_one_answer_and_is_harmless_at_the_start():
    answers = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "family"))

    assert engine.back(answers) == answers[:1]
    assert engine.back(()) == ()


def test_english_buttons_for_an_english_customer():
    answers = walk(SEVEN, (BUDGET, ANY), lang="en")

    step = engine.next_step(PHONES, SEVEN, answers, lang="en")

    assert isinstance(step, Ask) and step.question == PURPOSE
    assert step.ask == "What will you mainly use it for?"
    assert step.choices[0].label == "For studies"
    chosen = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "study"), lang="en")
    assert chosen[-1].label == "For studies", "the model is told what the button said"


def test_sizes_read_in_order_and_not_by_how_many_there_are():
    televisions = BY_SHELF["tv"]
    items = tuple(
        phone(f"t{n}", 30_000 + n, brand="LG").__class__(
            item_code=f"t{n}",
            name=f"TV {n}",
            brand="LG",
            price=30_000 + n,
            attributes={"brand": "LG", "size": size},
        )
        for n, size in enumerate(
            ["50–59 inch"] * 3 + ["32–39 inch"] + ["40–49 inch"] * 2
        )
    )

    step = engine.next_step(televisions, items, ())

    assert isinstance(step, Ask) and step.question == "size"
    assert [c.id for c in step.choices][:3] == [
        "32–39 inch",
        "40–49 inch",
        "50–59 inch",
    ]


def test_an_engineering_student_is_asked_what_matters_not_told():
    """The owner's brief: "நான் engineer" must not become a gaming phone with
    more RAM. The course is asked; the priority is asked plainly after it."""
    answers = walk(SEVEN, (BUDGET, ANY), (PURPOSE, "study"), (USE, "course"))

    step = engine.next_step(PHONES, SEVEN, answers)

    assert isinstance(step, Ask) and step.question == PRIORITY
    assert step.note == "" and step.ask == "உங்களுக்கு எது முக்கியம்?"
    assert "சரி, அப்படியே பாரு" not in {c.label for c in step.choices}, (
        "nothing pre-picked for them"
    )


def test_keypad_phones_are_left_for_budgets_only_they_fit():
    """Live, 2026-11-24: a ₹1,399 keypad phone was a "site video" card."""
    keypad = phone("k", 1_399, brand="Itel")
    keypad = type(keypad)(
        **{**keypad.__dict__, "attributes": {"brand": "Itel", "kind": "keypad"}}
    )
    smart = phone("s", 16_999, brand="Samsung")
    items = (keypad, smart)

    anything = engine.matching(PHONES, items, (Answer(BUDGET, "cap:25000", "x"),))
    tiny = engine.matching(PHONES, items, (Answer(BUDGET, "cap:3000", "x"),))

    assert [i.item_code for i in anything] == ["s"]
    assert [i.item_code for i in tiny] == ["k"]


def test_a_brand_said_that_is_not_among_the_eight_buttons_is_still_taken():
    """Live: "Apple phone" was refused - Apple had stock but was not one of
    the eight most-stocked brands shown as buttons."""
    brands = ["A", "B", "C", "D", "E", "F", "G", "H"]
    items = tuple(
        phone(f"{b}{n}", 20_000 + n, brand=b) for b in brands for n in range(3)
    ) + (phone("apple1", 60_000, brand="Apple"),)

    shown = engine.next_step(PHONES, items, ())
    assert isinstance(shown, Ask) and "Apple" not in {c.id for c in shown.choices}

    answers = engine.answer(PHONES, items, (), BRAND, "apple")
    assert answers[-1] == Answer(BRAND, "Apple", "Apple")
