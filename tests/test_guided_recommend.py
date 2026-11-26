"""Five to show, and the one call that may be paid for to choose them.

The model is a fake throughout. What is tested is everything around it:
when it is not asked at all, when it is asked once for a hundred customers,
and what happens to an answer that should not be shown to anyone.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.guided.engine import Answer, Ready
from src.guided.recommend import (
    ALL,
    CACHE,
    MODEL,
    RULES,
    Recommender,
    ranked,
    reason,
    sound,
)
from src.guided.script import ANY, BRAND, BUDGET, BY_SHELF, PRIORITY
from src.guided.thinker import (
    MAX_OUTPUT_TOKENS,
    Brief,
    GeminiThinker,
    ThinkerUnavailable,
    read_reply,
    request_for,
    usd_for,
)
from tests.guided_fakes import FakeThinker, phone

PHONES = BY_SHELF["phone"]
CAMERA = next(p for p in PHONES.priorities if p.id == "camera")
ANSWERS = (
    Answer(BUDGET, ANY, "பட்ஜெட் பரவாயில்ல"),
    Answer(BRAND, ANY, "எந்த பிராண்டும் பரவாயில்ல"),
    Answer(PRIORITY, "camera", "கேமரா"),
)
GOOD = "இது 50MP+ கேமரா கொண்டுள்ளது."


def eight() -> tuple:
    """Eight phones the camera figure cannot tell apart - so ranking them
    for a camera buyer is a judgement, and the one place a think is paid."""
    return tuple(
        phone(str(n), 20_000 + n * 1_000, camera=50.0, battery=5000.0)
        for n in range(1, 9)
    )


async def test_a_priority_the_figures_settle_is_ranked_by_them_for_nothing():
    """50MP against 108MP needs no model: the bigger number goes first,
    the governor is never asked, and nothing is spent."""
    thinker = FakeThinker()
    asked: list[int] = []
    items = tuple(
        phone(str(n), 20_000 + n * 1_000, camera=50.0 if n < 6 else 108.0)
        for n in range(1, 9)
    )

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(items, "camera"), lambda: asked.append(1) or True
    )

    assert chosen.source == RULES and chosen.usd == 0.0
    assert {p.item.item_code for p in chosen.picks[:3]} == {"6", "7", "8"}
    assert thinker.briefs == [] and asked == []


def allowed() -> bool:
    return True


async def test_five_or_fewer_are_shown_without_asking_anyone_anything():
    thinker = FakeThinker()
    asked = []

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(eight()[:4], "camera"), lambda: asked.append(1) or True
    )

    assert chosen.source == ALL and chosen.usd == 0.0 and len(chosen.picks) == 4
    assert thinker.briefs == [] and asked == [], (
        "the governor was consulted for nothing"
    )


async def test_more_than_five_is_one_think_and_the_bill_for_it_is_reported():
    thinker = FakeThinker(tuple((str(n), GOOD) for n in (3, 1, 2, 5, 4)), usd=0.0007)

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), allowed
    )

    assert chosen.source == MODEL and chosen.usd == 0.0007
    assert [pick.ref for pick in chosen.picks] == ["1", "2", "3", "4"]
    assert len(thinker.briefs) == 1


async def test_the_same_answers_on_the_same_shelf_are_never_paid_for_twice():
    thinker = FakeThinker(tuple((str(n), GOOD) for n in range(1, 6)))
    recommender = Recommender(thinker)
    first = await recommender.choose(PHONES, ANSWERS, Ready(eight(), "camera"), allowed)

    again = await recommender.choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), lambda: False
    )

    assert again.source == CACHE and again.usd == 0.0 and again.picks == first.picks
    assert len(thinker.briefs) == 1


async def test_a_price_moving_on_the_shelf_is_a_new_question():
    thinker = FakeThinker(tuple((str(n), GOOD) for n in range(1, 6)))
    recommender = Recommender(thinker)
    await recommender.choose(PHONES, ANSWERS, Ready(eight(), "camera"), allowed)

    cheaper = (phone("1", 19_000, camera=50.0, battery=5000.0), *eight()[1:])
    chosen = await recommender.choose(
        PHONES, ANSWERS, Ready(cheaper, "camera"), allowed
    )

    assert chosen.source == MODEL and len(thinker.briefs) == 2


async def test_a_session_that_has_not_earned_a_think_still_gets_five_products():
    thinker = FakeThinker(tuple((str(n), GOOD) for n in range(1, 6)))

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), lambda: False
    )

    assert chosen.source == RULES and chosen.usd == 0.0 and len(chosen.picks) == 4
    assert thinker.briefs == [], "the model was called without the governor's leave"
    # Nothing to tell the cameras apart, so the unpaid order falls through
    # to its next rule; the camera-first order has its own test above.
    assert [p.item.item_code for p in chosen.picks] == ["1", "2", "3", "4"]


async def test_a_model_that_is_down_is_not_a_recommendation_that_fails():
    chosen = await Recommender(FakeThinker(down=True)).choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), allowed
    )

    assert chosen.source == RULES and len(chosen.picks) == 4


async def test_a_reply_that_was_paid_for_and_unusable_is_charged_and_not_remembered():
    thinker = FakeThinker((), usd=0.0006)
    recommender = Recommender(thinker)

    chosen = await recommender.choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), allowed
    )
    await recommender.choose(PHONES, ANSWERS, Ready(eight(), "camera"), allowed)

    assert chosen.source == RULES and chosen.usd == 0.0006 and len(chosen.picks) == 4
    assert len(thinker.briefs) == 2, "an empty answer was cached as the answer"


async def test_ids_outside_the_list_and_ids_named_twice_are_not_picks():
    thinker = FakeThinker(
        (("99", GOOD), ("2", GOOD), ("2", GOOD), ("0", GOOD), ("7", GOOD))
    )

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), allowed
    )

    codes = [pick.item.item_code for pick in chosen.picks]
    assert len(codes) == 4 == len(set(codes)), "topped up to a screenful, none twice"


@pytest.mark.parametrize(
    "why",
    [
        "இது 200MP கேமரா கொண்டுள்ளது.",  # a figure the card does not state
        "Great camera and long battery life.",  # not Tamil
        "இது ₹2,000 குறைவான விலையில் கிடைக்கிறது.",  # a price
        "இது Rs 500 off கொண்டுள்ளது.",
        # 50 IS on the card, as megapixels. Said as money it is a discount
        # nobody offered, and only the currency mark gives it away.
        "இதுக்கு ₹50 தள்ளுபடி கிடைக்கும்.",
        "ரூபாய் கணக்குல இது தான் நல்ல போன்.",  # no figure at all, still a price claim
        "",
        "நல்ல போன் " * 30,  # an essay
    ],
)
async def test_a_sentence_that_should_not_be_shown_is_replaced_by_one_built_from_the_card(
    why,
):
    thinker = FakeThinker(tuple((str(n), why) for n in range(1, 6)))

    chosen = await Recommender(thinker).choose(
        PHONES, ANSWERS, Ready(eight(), "camera"), allowed
    )

    assert len(chosen.picks) == 4
    for pick in chosen.picks:
        assert pick.why == reason(pick.item, CAMERA) != why


def test_a_sentence_may_quote_any_figure_on_its_own_card_or_in_its_own_name():
    item = phone("17", 30_000, camera=50.0, battery=5000.0)  # "Samsung Phone 17 5G"

    assert sound("இது 50MP+ கேமரா, 5,000mAh+ பேட்டரி மற்றும் 5G கொண்டுள்ளது.", item)
    assert not sound("இது 64MP கேமரா கொண்டுள்ளது.", item)


def test_the_unpaid_order_puts_what_was_asked_for_first_then_rating_then_reach():
    items = (
        phone("a", 20_000, camera=50.0, rating=4.9, branches=90),
        phone("b", 30_000, camera=108.0, rating=3.0, branches=1),
        phone("c", 25_000, camera=50.0, rating=4.9, branches=5),
        phone("d", 22_000, rating=5.0),  # states no camera at all
    )

    assert [i.item_code for i in ranked(items, CAMERA)] == ["b", "a", "c", "d"]
    assert [i.item_code for i in ranked(items, None)] == ["d", "a", "c", "b"]


def test_a_reason_leads_with_what_was_asked_for_and_states_a_saving_as_a_saving():
    item = phone("1", 24_999, camera=50.0, battery=5500.0, was=25_999)
    battery = next(p for p in PHONES.priorities if p.id == "battery")

    assert reason(item, battery) == "பேட்டரி 5500mAh+ · கேமரா 50MP+ · ₹1,000 குறைவு"
    assert reason(phone("2", 9_000), None) == "", "nothing known is nothing said"


# --- the call itself --------------------------------------------------------


def brief() -> Brief:
    return Brief(
        "phone", (("budget", "₹20,000 – ₹30,000"), ("priority", "கேமரா")), eight()
    )


def asked_of_the_model(sent: Brief) -> dict:
    """What the model is actually given to read, as data."""
    return json.loads(request_for(sent)["contents"][0]["parts"][0]["text"])


def test_the_model_is_never_shown_a_price_or_an_item_code():
    """Names, facts and ratings. Prices and codes are attached by code, after.

    A price it never saw cannot be misquoted as a discount, and an id that
    is only "3" cannot be the item code of some other product.
    """
    products = tuple(
        replace(
            phone(f"X90{n}7", 23_457 + n * 1_111, camera=50.0, was=31_999),
            name=f"Galaxy A{n}7 5G",
        )
        for n in range(1, 8)
    )
    sent = json.dumps(request_for(Brief("phone", (), products)), ensure_ascii=False)

    for item in products:
        assert str(item.price) not in sent, "a price reached the model"
        assert item.item_code not in sent, "an item code reached the model"
    assert "31999" not in sent and "₹" not in sent
    listed = asked_of_the_model(Brief("phone", (), products))["products"]
    assert [product["id"] for product in listed] == ["1", "2", "3", "4", "5", "6", "7"]
    assert listed[0] == {"id": "1", "name": "Galaxy A17 5G", "facts": {"கேமரா": "50MP+"}}


def test_what_the_shopper_chose_is_what_the_model_ranks_on():
    assert asked_of_the_model(brief())["shopper_chose"] == {
        "budget": "₹20,000 – ₹30,000",
        "priority": "கேமரா",
    }


def test_the_request_is_bounded_and_asks_for_json_at_temperature_zero():
    config = request_for(brief())["generationConfig"]

    assert config["temperature"] == 0.0
    assert config["maxOutputTokens"] == MAX_OUTPUT_TOKENS
    assert config["responseMimeType"] == "application/json"
    assert "thinkingConfig" not in config, "this model rejects the field outright"


def test_a_measured_think_prices_at_seven_paise():
    """1,192 in and 134 out, on flash-lite's card, at Rs 105.02 a dollar."""
    assert usd_for(1192, 134, 0) * 89.0 * 1.18 == pytest.approx(0.073, abs=0.001)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"candidates": []},
        {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]},
        {
            "candidates": [
                {"content": {"parts": [{"text": '{"picks": "all of them"}'}]}}
            ]
        },
    ],
)
def test_a_reply_that_is_not_what_was_asked_for_is_unavailable_rather_than_guessed_at(
    body,
):
    with pytest.raises(ThinkerUnavailable):
        read_reply(body)


class Reply:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code, self._body = status, body

    def json(self) -> dict:
        return self._body


class Recorder:
    def __init__(self, reply: Reply | Exception) -> None:
        self.reply, self.calls = reply, []

    async def post(self, url, *, json=None, headers=None):  # noqa: A002
        self.calls.append((url, headers))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


KEY = "AIzaSy-synthetic-not-a-real-key-000000000"


async def test_the_key_travels_in_a_header_and_the_bill_is_read_off_the_reply():
    text = json.dumps({"picks": [{"id": "2", "why": GOOD}]}, ensure_ascii=False)
    poster = Recorder(
        Reply(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": text}]}}],
                "usageMetadata": {
                    "promptTokenCount": 1192,
                    "candidatesTokenCount": 134,
                },
            },
        )
    )

    thought = await GeminiThinker(
        KEY, "text-model", poster_factory=lambda: poster
    ).rank(brief())

    ((url, headers),) = poster.calls
    assert KEY not in url and headers == {"x-goog-api-key": KEY}
    assert thought.picks == (("2", GOOD),)
    assert thought.usd == pytest.approx(usd_for(1192, 134, 0))


@pytest.mark.parametrize(
    "reply", [Reply(429, {}), Reply(500, {}), TimeoutError(f"GET ?key={KEY}")]
)
async def test_a_failed_call_says_so_without_repeating_the_key(reply):
    thinker = GeminiThinker(KEY, "text-model", poster_factory=lambda: Recorder(reply))

    with pytest.raises(ThinkerUnavailable) as failed:
        await thinker.rank(brief())

    assert KEY not in str(failed.value) and failed.value.__cause__ is None


async def test_a_reply_that_cannot_be_read_still_reports_what_it_cost():
    poster = Recorder(
        Reply(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "sorry, I cannot"}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 20},
            },
        )
    )

    thought = await GeminiThinker(
        KEY, "text-model", poster_factory=lambda: poster
    ).rank(brief())

    assert thought.picks == () and thought.usd > 0
