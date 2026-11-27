"""A guided call from the first question to the product card, over HTTP.

The real routes, the real flow, the real engine - with the index, the estate
and the model replaced by fakes, so the whole walk runs offline and costs
nothing. What is being checked is the brief: the questions are free, only
the last step can spend, and nobody can make it spend by hurrying.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import load
from src.guided.flow import Flow
from src.guided.recommend import Recommender
from src.guided.script import line
from src.guided.sessions import Governor, Sessions
from src.guided.shelf import ShelfBook
from src.web.app import create_app
from tests.guided_fakes import FakeEstate, FakeIndex, FakeThinker, indexed, on_shelf

GOOD = "இது 45MP+ கேமரா கொண்டுள்ளது."

# The full ladder into the 20-30k band, as the owner walks it: brand,
# budget, what for, the suggestion accepted, and "a little above is fine".
NARROW = (
    ("shelf", "phone"),
    ("brand", "any"),
    ("budget", "20000-30000"),
    ("purpose", "family"),
    ("priority", "value"),
    ("confirm", "yes"),
    ("budget_up", "yes"),
)
WIDE = (
    ("shelf", "phone"),
    ("brand", "any"),
    ("budget", "any"),
    ("purpose", "fun"),
    ("use", "camera"),
    # "Value" is the one priority with no number to sort by, so it is the
    # one that earns the think; camera, battery, RAM and storage are
    # ranked by their measured figure for nothing.
    ("priority", "value"),
    ("confirm", "yes"),
)


def phones(count: int) -> tuple[list[dict], dict[str, dict]]:
    """`count` phones in stock from 12,000 up, two brands, cameras that differ."""
    documents, estate = [], {}
    for n in range(count):
        code = str(100 + n)
        documents.append(
            indexed(
                code,
                f"Phone {code} 5G",
                99_999,  # the index's price is wrong on purpose, everywhere
                brand="Samsung" if n % 2 else "Vivo",
                primary_camera=["45.1MP - 59.9MP" if n % 3 else "12MP - 29.9MP"],
            )
        )
        estate[code] = on_shelf(code, 12_000 + n * 4_000, mrp=14_000 + n * 4_000)
    return documents, estate


class Rig:
    """One app with a guided flow built on fakes, and a clock that can be moved."""

    def __init__(
        self,
        monkeypatch,
        count: int = 8,
        thinker: FakeThinker | None = None,
        recordings=None,
        answerer=None,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
        monkeypatch.setenv("CATALOGUE_TOKEN", "")
        self.now = [1_000.0]
        clock = lambda: self.now[0]  # noqa: E731
        documents, answers = phones(count)
        self.index = FakeIndex({"phone": documents, "tv": []})
        self.estate = FakeEstate(answers)
        self.thinker = thinker or FakeThinker(
            tuple((str(n), GOOD) for n in range(1, 6))
        )
        self.sessions = Sessions(clock=clock)
        flow = Flow(
            ShelfBook(self.index, self.estate, clock=clock),
            Recommender(self.thinker),
            self.sessions,
            Governor(usd_inr=89.0, gst_rate=0.18, clock=clock),
            self.estate,
            recordings,
            answerer,
        )
        app = create_app(load("development"))
        app.state.guided = flow
        self.client = TestClient(app)

    def post(self, path: str, **body) -> dict:
        response = self.client.post(f"/api/v1/guided/{path}", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def verify(self, session: str) -> None:
        """As a signed-in, verified account would be: the only kind of
        session the one paid think is for (the owner's spec)."""
        self.sessions.get(session).verified = True

    def walk(self, *taps: tuple[str, str], lang: str = "ta") -> dict:
        view = self.post("start")
        view = self.post(
            "answer", session=view["session"], question="language", choice=lang
        )
        for question, choice in taps:
            view = self.post(
                "answer", session=view["session"], question=question, choice=choice
            )
        return view


@pytest.fixture
def rig(monkeypatch) -> Rig:
    return Rig(monkeypatch)


def voiced(tmp_path, *keys: str):
    """A recordings folder holding exactly these lines, current and playable.

    The real folder is whatever the recording quota allowed today; what
    these tests check is the plumbing from a step to its recording's name,
    which must not depend on which lines are on disk this afternoon.
    """
    from src.guided.script import SPOKEN
    from src.guided.voice import Recordings, wav, write_manifest

    write_manifest({key: {"text": SPOKEN[key]} for key in keys}, tmp_path)
    for key in keys:
        (tmp_path / f"{key}.wav").write_bytes(wav(b"\x01\x00" * 2_400, 24_000))
    return Recordings(tmp_path)


def test_every_step_names_the_recording_of_what_it_says(monkeypatch, tmp_path):
    """And the line to play if the next step keeps the customer waiting."""
    rig = Rig(
        monkeypatch,
        recordings=voiced(
            tmp_path,
            "ask_language",
            "ask_shelf",
            "ask_brand",
            "ask_budget",
            "ask_purpose",
            "ask_spec",
            "ask_confirm",
            "ask_budget_up",
            "say_thinking",
            "say_suggest",
            "say_card",
        ),
    )
    first = rig.post("start")
    session = first["session"]
    views = [first]
    for question, choice in (
        ("language", "ta"),
        ("shelf", "phone"),
        ("brand", "any"),
        ("budget", "20000-30000"),
        ("purpose", "family"),
        ("priority", "value"),
        ("confirm", "yes"),
        ("budget_up", "yes"),
    ):
        views.append(
            rig.post("answer", session=session, question=question, choice=choice)
        )
    views.append(rig.post("recommend", session=session))
    views.append(rig.post("pick", session=session, ref="1"))

    assert [view["audio"] for view in views] == [
        "ask_language",
        "ask_shelf",
        "ask_brand",
        "ask_budget",
        "ask_purpose",
        "ask_spec",
        "ask_confirm",
        "ask_budget_up",
        "say_thinking",
        "say_suggest",
        "say_card",
    ]
    assert first["waiting"] == {"say": "ஒரு நிமிஷம், பாக்குறேன்.", "audio": "say_thinking"}


def test_an_english_step_names_the_english_recording(monkeypatch, tmp_path):
    rig = Rig(
        monkeypatch, recordings=voiced(tmp_path, "ask_shelf_en", "say_thinking_en")
    )
    view = rig.walk(lang="en")

    assert view["question"]["id"] == "shelf" and view["audio"] == "ask_shelf_en"
    assert view["waiting"]["audio"] == "say_thinking_en"


def test_a_line_with_no_current_recording_is_shown_and_nothing_is_named(
    monkeypatch, tmp_path
):
    from src.guided.voice import Recordings

    rig = Rig(monkeypatch, recordings=Recordings(tmp_path))

    view = rig.walk(("shelf", "phone"))

    assert view["question"]["ask"] == "எந்த பிராண்ட் வேணும்?"
    assert view["audio"] is None and view["waiting"]["audio"] is None


def test_a_recording_is_fetched_as_sound_and_nothing_else_can_be_fetched_that_way(
    rig: Rig,
):
    clip = rig.client.get("/guided/voice/ask_budget.wav")

    assert clip.status_code == 200 and clip.headers["content-type"].startswith("audio/")
    assert clip.content[:4] == b"RIFF"
    for elsewhere in (
        "/guided/voice/../voice.py",
        "/guided/voice/%2e%2e/voice.py",
        "/guided/voice/%2e%2e%2f%2e%2e%2f%2e%2e%2f.env",
        "/guided/voice/nothing.wav",
    ):
        assert rig.client.get(elsewhere).status_code == 404, elsewhere


def test_the_first_question_is_the_language_and_the_second_the_shelf(rig: Rig):
    view = rig.post("start")

    assert view["step"] == "question" and view["question"]["id"] == "language"
    assert [c["id"] for c in view["question"]["choices"]] == ["ta", "en"]

    view = rig.post("answer", session=view["session"], question="language", choice="ta")
    assert view["question"]["id"] == "shelf"
    offered = {c["id"]: c["label"] for c in view["question"]["choices"]}
    assert offered["phone"] == "போன்"
    assert view["meter"]["total_inr"] == 0.0 and rig.thinker.briefs == []


def test_an_english_customer_gets_the_call_in_english(rig: Rig):
    view = rig.walk(("shelf", "phone"), lang="en")

    assert view["lang"] == "en" and view["question"]["id"] == "brand"
    assert view["question"]["ask"] == "Which brand do you prefer?"
    assert view["answered"][0]["label"] == "Phone"
    assert view["question"]["choices"][-1]["label"] == "Any brand is fine"

    view = rig.post("answer", session=view["session"], question="brand", choice="any")
    assert view["question"]["ask"] == "What is the most you would like to spend?"


def test_budget_buttons_are_counted_on_the_real_price_not_the_index_price(rig: Rig):
    """Every phone is 99,999 in the index and 12-40k at the till."""
    view = rig.walk(("shelf", "phone"), ("brand", "any"))

    assert view["question"]["id"] == "budget"
    drawn = {c["id"]: c["count"] for c in view["question"]["choices"]}
    assert drawn == {
        "10000-15000": 1,
        "15000-20000": 1,
        "20000-30000": 3,
        "30000-50000": 3,
        "any": 8,
    }
    assert view["answered"][0] == {"question": "shelf", "label": "போன்"}


def test_a_narrow_budget_goes_straight_to_the_products_and_no_model_is_asked(rig: Rig):
    view = rig.walk(*NARROW)
    assert view["step"] == "ready" and view["left"] == 3

    result = rig.post("recommend", session=view["session"])

    assert result["step"] == "result" and result["suggesting"] is True
    assert len(result["picks"]) == 4, "four on screen; only the best is spoken"
    assert {way["id"] for way in result["more"]} == {"next", "list"}
    assert result["meter"]["total_inr"] == 0.0 and rig.thinker.briefs == []

    laid_out = rig.post("refine", session=view["session"], kind="list")
    assert [pick["price"] for pick in laid_out["picks"]] == [
        20_000,
        24_000,
        28_000,
        32_000,
    ]
    over = [pick["over_budget"] for pick in laid_out["picks"]]
    assert over == [None, None, None, "உங்க பட்ஜெட்டை விட கொஞ்சம் அதிகம்"], (
        "the one just over the budget says so, and only that one"
    )


def test_a_customer_who_took_their_time_gets_the_one_paid_think(rig: Rig):
    view = rig.walk(*WIDE)
    assert view["step"] == "ready" and view["left"] == 8
    rig.verify(view["session"])

    rig.now[0] += 30.0
    result = rig.post("recommend", session=view["session"])

    assert result["how"]["source"] == "model" and len(result["picks"]) == 4
    assert result["suggesting"] is True
    assert result["meter"]["total_inr"] == pytest.approx(0.074, abs=0.001)
    assert result["meter"]["total_inr_per_min"] < 1.50
    assert len(rig.thinker.briefs) == 1
    wants = list(rig.thinker.briefs[0].wants)
    assert wants[-2] == ("use", "போட்டோ, ரீல்ஸ்")
    assert wants[-1] == ("priority", "விலைக்கு ஏத்த மதிப்பு")


def test_a_script_that_hurries_gets_five_products_and_spends_nothing(rig: Rig):
    """No time on the line, no think. The answer is still an answer."""
    view = rig.walk(*WIDE)

    result = rig.post("recommend", session=view["session"])

    assert result["how"]["source"] == "rules" and len(result["picks"]) == 4
    assert result["meter"]["total_inr"] == 0.0 and rig.thinker.briefs == []


def test_asking_again_and_again_is_paid_for_once(rig: Rig):
    taps = WIDE
    for _ in range(6):
        view = rig.walk(*taps)
        rig.verify(view["session"])
        rig.now[0] += 30.0
        rig.post("recommend", session=view["session"])
        rig.post("recommend", session=view["session"])

    assert len(rig.thinker.briefs) == 1


def test_picking_one_asks_the_estate_again_and_shows_what_it_says_now(
    rig: Rig, monkeypatch
):
    # The price moves between the cards and the tap, inside a second here.
    # A tap normally joins the ask made when the cards appeared (at most
    # 25 s, under the estate's own 30 s cache); with no window, it re-asks.
    monkeypatch.setattr("src.guided.flow.PREFETCH_SECONDS", 0.0)
    view = rig.walk(*NARROW)
    result = rig.post("recommend", session=view["session"])
    code = "102"  # 20,000 when the shelf was built
    rig.estate.by_code[code] = {
        **on_shelf(code, 19_499, mrp=22_000, quantity=2, branches=1),
        "sections": {
            **on_shelf(code, 19_499, mrp=22_000, quantity=2, branches=1)["sections"],
            "emi": {"status": "ok", "data": {"provider_count": 4}},
        },
        "summary": {
            "selling_price": 19_499,
            "mrp": 22_000,
            "emi_starts_from": {
                "per_month": 1_625,
                "months": 12,
                "provider": "HDFC",
                "type": "No Cost",
            },
        },
    }

    card = rig.post("pick", session=view["session"], ref=result["picks"][0]["ref"])

    product = card["product"]
    assert card["step"] == "card" and product["checked_live"] is True
    assert (product["price"], product["was"]) == (19_499, 22_000)
    assert product["stock"] == {"state": "in", "quantity": 2, "branches": 1}
    assert product["emi"]["per_month"] == 1_625 and product["emi"]["months"] == 12
    assert card["meter"]["total_inr"] == 0.0


def test_a_pick_the_estate_will_not_answer_for_shows_what_the_shelf_knew(
    rig: Rig, monkeypatch
):
    # The price moves between the cards and the tap, inside a second here.
    # A tap normally joins the ask made when the cards appeared (at most
    # 25 s, under the estate's own 30 s cache); with no window, it re-asks.
    monkeypatch.setattr("src.guided.flow.PREFETCH_SECONDS", 0.0)
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    rig.estate.by_code.clear()

    product = rig.post("pick", session=view["session"], ref="1")["product"]

    assert product["checked_live"] is False and product["price"] == 20_000
    assert product["stock"]["state"] == "unknown", (
        "no live answer, so no stock claim - not the shelf's older word"
    )
    assert product["emi"] is None and product["offers"] == []


def test_a_ref_that_was_never_shown_picks_nothing(rig: Rig):
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])

    assert rig.post("pick", session=view["session"], ref="9")["step"] == "ready"


def test_an_answer_that_is_not_a_button_changes_nothing(rig: Rig):
    view = rig.walk(("shelf", "phone"))

    for question, choice in (
        ("budget", "1-99999999"),
        ("brand", "Nokia"),
        ("shelf", "tv"),
        ("priority", "camera"),
        ("purpose", "gaming"),
    ):
        after = rig.post(
            "answer", session=view["session"], question=question, choice=choice
        )
        assert (
            after["question"] == view["question"]
            and after["answered"] == view["answered"]
        )


def test_back_undoes_one_tap_at_a_time_all_the_way_to_the_first_question(rig: Rig):
    view = rig.walk(
        ("shelf", "phone"), ("brand", "any"), ("budget", "any"), ("purpose", "family")
    )
    session = view["session"]

    assert rig.post("back", session=session)["question"]["id"] == "purpose"
    assert rig.post("back", session=session)["question"]["id"] == "budget"
    assert rig.post("back", session=session)["question"]["id"] == "brand"
    assert rig.post("back", session=session)["question"]["id"] == "shelf"
    assert rig.post("back", session=session)["question"]["id"] == "language"
    assert rig.post("back", session=session)["question"]["id"] == "language"


def test_an_empty_shelf_says_so_at_once_and_is_then_left_off_the_first_question(
    rig: Rig,
):
    view = rig.walk(("shelf", "tv"))

    assert view["step"] == "nothing" and view["say"] == line("say_nothing")
    again = rig.post("start")
    assert "tv" not in {choice["id"] for choice in again["question"]["choices"]}


def test_recommend_before_the_questions_are_done_puts_the_question_again(rig: Rig):
    view = rig.walk(("shelf", "phone"))

    assert rig.post("recommend", session=view["session"])["question"]["id"] == "brand"
    assert rig.thinker.briefs == []


def test_a_session_nobody_was_given_is_a_404_in_the_one_error_shape(rig: Rig):
    response = rig.client.post(
        "/api/v1/guided/answer",
        json={"session": "A" * 22, "question": "shelf", "choice": "phone"},
    )

    assert response.status_code == 404
    assert response.json()["type"] == "error" and response.json()["message"] == line(
        "say_expired"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"session": "short", "question": "shelf", "choice": "phone"},
        {"session": "A" * 22, "question": "DROP TABLE", "choice": "phone"},
        {"session": "A" * 22, "question": "shelf", "choice": "x" * 81},
        {"session": "../../etc/passwd/aaaa", "question": "shelf", "choice": "phone"},
        {"question": "shelf", "choice": "phone"},
    ],
)
def test_a_body_that_is_not_a_tap_never_reaches_the_flow(rig: Rig, body: dict):
    assert rig.client.post("/api/v1/guided/answer", json=body).status_code == 422
    assert rig.index.reads == []


def test_thinks_are_limited_per_client_whatever_the_governor_would_allow(rig: Rig):
    view = rig.walk(*NARROW)
    statuses = [
        rig.client.post(
            "/api/v1/guided/recommend", json={"session": view["session"]}
        ).status_code
        for _ in range(14)
    ]

    assert statuses[:12] == [200] * 12 and statuses[12:] == [429, 429]
    refused = rig.client.post(
        "/api/v1/guided/recommend", json={"session": view["session"]}
    )
    assert refused.json()["message"] == line("say_slow_down")


def test_a_catalogue_that_is_down_is_said_as_that_and_not_as_an_empty_shop(rig: Rig):
    rig.index.down = True
    view = rig.post("start")
    view = rig.post("answer", session=view["session"], question="language", choice="ta")

    response = rig.client.post(
        "/api/v1/guided/answer",
        json={"session": view["session"], "question": "shelf", "choice": "phone"},
    )

    assert response.status_code == 503 and response.json()["message"] == line(
        "say_down"
    )


def test_with_no_catalogue_configured_the_guided_call_is_unavailable_not_broken(
    monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("CATALOGUE_TOKEN", "")
    client = TestClient(create_app(load("development")))

    response = client.post("/api/v1/guided/start")

    assert response.status_code == 503 and response.json()["message"] == line(
        "say_down"
    )


def test_nothing_the_page_is_sent_names_the_model_or_an_item_code(rig: Rig):
    view = rig.walk(*WIDE)
    rig.now[0] += 30.0
    result = rig.post("recommend", session=view["session"])
    card = rig.post("pick", session=view["session"], ref="1")

    sent = (str(view) + str(result) + str(card)).lower()
    for word in ("gemini", "flash", "google", "item_code", "'10"):
        assert word not in sent, word


# --- the customer's own questions, through the whole stack -------------------


class OneAnswer:
    """A model that always answers the same grounded sentence."""

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, instruction, said, schema, max_output_tokens):
        from src.guided.model import Reply

        self.asked += 1
        # 20000 is the price of item 102 and appears in its facts.
        return Reply(
            {"known": True, "answer": "இது ₹இல்லாம 20000 ரூபாய் தான்."},
            1500,
            30,
            0,
            0.0005,
            700,
        )


def asking_rig(monkeypatch) -> tuple[Rig, dict]:
    from src.guided.answers import Answerer

    answers = OneAnswer()
    rig = Rig(monkeypatch, answerer=Answerer(answers))
    view = rig.walk(*NARROW)
    # A paid question is for a verified shopper. A guest is answered without
    # a model call.
    rig.verify(view["session"])
    rig.post("recommend", session=view["session"])
    card = rig.post("pick", session=view["session"], ref="1")
    rig.model = answers  # type: ignore[attr-defined]
    return rig, card


def test_a_question_about_the_pick_is_answered_and_billed_once(monkeypatch):
    rig, card = asking_rig(monkeypatch)
    rig.now[0] += 60.0

    first = rig.post("ask", session=card["session"], question="warranty")
    again = rig.post("ask", session=card["session"], question="warranty")

    assert first["step"] == "answer" and first["known"] is True
    assert "20000" in first["say"]
    assert first["how"]["source"] == "model" and first["how"]["inr"] > 0
    assert again["how"]["source"] == "cache" and again["how"]["inr"] == 0.0
    assert rig.model.asked == 1


def test_six_questions_then_everyone_is_sent_to_the_shop(monkeypatch):
    rig, card = asking_rig(monkeypatch)
    rig.now[0] += 300.0

    # Only questions the card cannot answer itself count against the six;
    # EMI, branches, colours, offers and price are read off the card free.
    free = rig.post("ask", session=card["session"], question="where")
    assert free["how"]["source"] == "rules" and free["left"] == 6

    kinds = ["warranty", "box"] * 4
    answers = [rig.post("ask", session=card["session"], question=k) for k in kinds]

    assert [a["left"] for a in answers[:6]] == [5, 4, 3, 2, 1, 0]
    assert all(a["how"]["source"] == "capped" for a in answers[6:])
    assert "பக்கத்து கடைல" in answers[7]["say"]
    assert rig.model.asked <= 6


def test_a_question_that_is_not_a_button_is_not_looked_at_and_not_counted(monkeypatch):
    rig, card = asking_rig(monkeypatch)
    rig.now[0] += 60.0

    refused = rig.post("ask", session=card["session"], question="weather_x")

    assert refused["known"] is False and refused["left"] == 6
    assert rig.model.asked == 0
    body = rig.client.post(
        "/api/v1/guided/ask",
        json={"session": card["session"], "question": "DROP TABLE; --"},
    )
    assert body.status_code == 422, "free text never reaches the flow at all"


def test_nothing_picked_means_nothing_to_ask_about(monkeypatch):
    from src.guided.answers import Answerer

    answers = OneAnswer()
    rig = Rig(monkeypatch, answerer=Answerer(answers))
    view = rig.walk(*NARROW)

    put_back = rig.post("ask", session=view["session"], question="emi")

    assert put_back["step"] == "ready" and answers.asked == 0


def test_a_hurried_question_is_sent_to_the_shop_not_the_model(monkeypatch):
    rig, card = asking_rig(monkeypatch)
    # No time has passed: the governor refuses, and the refusal is honest -
    # "ask at the shop", not "I am not sure".
    refused = rig.post("ask", session=card["session"], question="warranty")

    assert refused["how"]["source"] == "refused" and rig.model.asked == 0
    assert "கடைல" in refused["say"]


def test_four_then_the_next_four_then_the_list_of_ten(monkeypatch):
    """The spec's walk: four on screen with the best one spoken; "இன்னும்
    காட்டு" - the next four; "பத்து காட்டு" - ten; the end is honest."""
    rig = Rig(monkeypatch, count=14)
    view = rig.walk(("shelf", "phone"), ("brand", "any"), ("budget", "any"))
    while view["step"] == "question":  # purpose chain and read-back, quickly
        view = rig.post(
            "answer",
            session=view["session"],
            question=view["question"]["id"],
            choice=view["question"]["choices"][0]["id"],
        )
    first = rig.post("recommend", session=view["session"])
    assert first["suggesting"] is True
    assert [p["ref"] for p in first["picks"]] == ["1", "2", "3", "4"]

    second = rig.post("refine", session=view["session"], kind="next")
    assert [p["ref"] for p in second["picks"]] == ["5", "6", "7", "8"], (
        "the NEXT four, not the same four"
    )

    listed = rig.post("refine", session=view["session"], kind="list")
    assert listed["say"] == "சரி, லிஸ்ட்டா காட்றேன். பார்த்து ஒண்ணை தொடுங்க."
    assert [p["ref"] for p in listed["picks"]] == [str(n) for n in range(5, 15)], (
        "ten, and none of the four turned down with 'வேற காட்டு'"
    )

    done = rig.post("refine", session=view["session"], kind="more")
    assert done["say"] == "ஸ்டாக்ல இருக்கறது அவ்வளவு தான். கீழ இருக்கறதுல தேர்ந்தெடுங்க."
    assert len(done["picks"]) == 10, "the list stays for choosing"

    card = rig.post("pick", session=view["session"], ref="12")
    assert card["step"] == "card", "a double-digit ref picks like any other"


def test_walking_past_the_last_four_lays_the_list_out(monkeypatch):
    rig = Rig(monkeypatch, count=6)
    view = rig.walk(*WIDE)
    rig.now[0] += 30.0
    rig.post("recommend", session=view["session"])

    last = {}
    for _ in range(3):
        last = rig.post("refine", session=view["session"], kind="next")
    assert last["say"] == "ஸ்டாக்ல இருக்கறது அவ்வளவு தான். கீழ இருக்கறதுல தேர்ந்தெடுங்க."
    assert len(last["picks"]) == 6


def test_a_brand_the_shelf_no_longer_holds_is_said_and_asked_again(monkeypatch):
    """The owner's script: "இந்த budget-ல இந்த brand இல்ல, வேற brand choose
    பண்ணுங்க." Buttons with honest counts cannot produce the clash by tap,
    so it arises when stock MOVES under answers already given - and then
    the brand is taken back, the budget stays, and the sentence is said."""
    from src.guided.engine import Answer

    rig = Rig(monkeypatch)
    view = rig.walk(("shelf", "phone"))
    session = rig.client.app.state.guided._sessions.get(view["session"])
    # Given honestly earlier; the shelf has since stopped matching them.
    session.answers = (
        Answer("brand", "Nokia", "Nokia"),
        Answer("budget", "20000-30000", "x"),
    )

    healed = rig.post("answer", session=view["session"], question="brand", choice="zz")

    assert healed["say"] == (
        "அந்த பிராண்ட்ல இந்த பட்ஜெட்டுக்குள்ள இப்போ எதுவும் இல்ல. இருக்கற பிராண்ட்ல ஒண்ணு சொல்லுங்க."
    )
    answered = [a["question"] for a in healed["answered"]]
    assert "budget" in answered and "brand" not in answered, "the budget stayed"


def test_the_dealer_step_names_the_shops_holding_stock(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    card = rig.post("pick", session=view["session"], ref="1")

    offered = {c["id"] for c in card["questions"]["choices"]}
    assert "dealer" in offered

    dealer = rig.post("ask", session=view["session"], question="dealer")
    assert dealer["step"] == "dealer"
    assert "நம்ம ஆள்" in dealer["say"]
    assert dealer["branches"], "the shops came from the live stock answer"
    assert all(shop["name"] for shop in dealer["branches"])
    # Not a model question: the six are untouched and nothing was spent.
    assert dealer["meter"]["total_inr"] == 0.0


def test_every_level_of_the_purpose_tree_can_be_tapped_through_the_route(rig: Rig):
    """work -> IT -> developer is three levels, the last asked as "use2".
    Tapped through HTTP, not the engine, so a request pattern that refuses
    a question id is caught here - it reached the owner once."""
    view = rig.walk(("shelf", "phone"), ("brand", "any"), ("budget", "any"))
    assert view["question"]["id"] == "purpose"
    for question, choice in (("purpose", "work"), ("use", "it"), ("use2", "dev")):
        assert view["question"]["id"] == question
        view = rig.post(
            "answer", session=view["session"], question=question, choice=choice
        )
    assert view["question"]["id"] == "priority"


def test_the_cards_on_screen_are_asked_about_before_anyone_taps(rig: Rig):
    """A tap then finds its live facts cached instead of waiting 2-3 s for
    a cold ask (owner, 2026-11-24: "make it faster"). Once per card."""
    view = rig.walk(*NARROW)
    rig.estate.asked.clear()

    result = rig.post("recommend", session=view["session"])
    shown = {pick["name"] for pick in result["picks"]}
    asked_early = {code for code, sections in rig.estate.asked if "emi" in sections}

    assert len(asked_early) == len(shown), "every card on screen, once"
    rig.post("refine", session=view["session"], kind="list")
    again = [code for code, sections in rig.estate.asked if "emi" in sections]
    assert len(again) == len(set(again)), "not asked twice in quick succession"


def test_a_guest_gets_the_whole_flow_ranked_by_rule_and_never_the_think(rig: Rig):
    """The owner's spec: the bounded text call is for verified accounts; a
    guest still gets four cards, at ₹0, however long they take."""
    view = rig.walk(*WIDE)
    rig.now[0] += 300.0

    result = rig.post("recommend", session=view["session"])

    assert result["how"]["source"] == "rules" and len(result["picks"]) == 4
    assert result["meter"]["total_inr"] == 0.0 and rig.thinker.briefs == []


def test_asking_for_the_dealer_by_voice_reaches_the_dealer(rig: Rig):
    """ "டீலர்கிட்ட பேசணும்" was heard, then dropped: it is not one of the
    suggested card questions, and only those were let through."""
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    rig.post("pick", session=view["session"], ref="1")

    spoken = rig.client.post(
        "/api/v1/guided/said",
        json={"session": view["session"], "text": "டீலர்கிட்ட பேசணும்"},
    ).json()

    assert spoken["step"] == "dealer"


def test_a_tap_asks_again_when_the_early_ask_came_back_empty(rig: Rig):
    """Live, 2026-11-24: a busy estate timed the early ask out, the tap
    joined that empty answer, and the card showed no live price or EMI."""
    view = rig.walk(*NARROW)
    code = "102"
    kept = rig.estate.by_code.pop(code)  # the early ask finds nothing
    rig.post("recommend", session=view["session"])
    rig.estate.by_code[code] = kept  # by the tap, the estate answers

    card = rig.post("pick", session=view["session"], ref="1")["product"]

    assert card["checked_live"] is True
