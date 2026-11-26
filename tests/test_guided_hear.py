"""Speech presses buttons. Only buttons, and only the ones that exist.

The ear is a fake that "hears" a list of presses. Everything after it is
real: the flow presses them through the same checks a tap goes through, in
the order the engine asks, and anything that is not a button does nothing.
"""

from __future__ import annotations

import base64
import json

import pytest

from src.guided.hear import (
    LEAST_BYTES,
    MOST_BYTES,
    MOST_PRESSES,
    MOST_TURNS,
    Heard,
    Listener,
    Press,
    read,
    request,
)
from src.guided.script import line
from tests.test_guided_flow import Rig

PCM = b"\x10\x00" * 16_000  # one second


class FakeListener:
    paid = False

    def __init__(self, *heard: Heard) -> None:
        self.heard = list(heard)
        self.menus: list[dict] = []

    async def hear(self, menu: dict, pcm: bytes) -> Heard:
        self.menus.append(menu)
        return self.heard.pop(0)


def pressing(*presses: tuple[str, str], question: str = "", text: str = "...") -> Heard:
    return Heard(text, tuple(Press(q, c) for q, c in presses), question, 0.0002, 900)


def hearing_rig(monkeypatch, *heard: Heard) -> tuple[Rig, FakeListener]:
    listener = FakeListener(*heard)
    rig = Rig(monkeypatch)
    rig.client.app.state.guided._listener = listener  # type: ignore[attr-defined]
    return rig, listener


def say(rig: Rig, session: str, pcm: bytes = PCM) -> dict:
    response = rig.client.post(
        f"/api/v1/guided/hear?session={session}",
        content=pcm,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- the request and the reply ------------------------------------------------


def test_the_ear_is_sent_the_audio_as_wav_and_the_menu_as_data():
    body = request({"now": {"question": "budget", "buttons": []}}, PCM)

    audio, menu = body["contents"][0]["parts"]
    wav = base64.b64decode(audio["inlineData"]["data"])
    assert wav[:4] == b"RIFF" and int.from_bytes(wav[24:28], "little") == 16_000
    assert json.loads(menu["text"]) == {
        "menu": {"now": {"question": "budget", "buttons": []}}
    }
    assert body["generationConfig"]["temperature"] == 0.0
    assert (
        "Never return a question or id that is not on an item"
        in (body["systemInstruction"]["parts"][0]["text"])
    )


def test_a_reply_is_read_into_presses_and_bounded():
    text = json.dumps(
        {
            "heard": "x" * 900,
            "presses": [{"question": "budget", "choice": "any"}] * 20,
            "question": "",
        }
    )
    got = read({"candidates": [{"content": {"parts": [{"text": text}]}}]})

    assert got is not None
    heard, presses, question = got
    assert len(heard) == 300 and len(presses) == MOST_PRESSES and question == ""


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"candidates": []},
        {"candidates": [{"content": {"parts": [{"text": "no"}]}}]},
    ],
)
def test_a_reply_that_is_not_json_is_not_understood(reply):
    assert read(reply) is None


async def test_an_ear_that_is_down_is_not_understood_rather_than_an_error():
    from src.guided.model import ModelUnavailable

    class Down:
        async def call(self, body):
            raise ModelUnavailable("the ear returned 503")

    heard = await Listener(Down()).hear({}, PCM)

    assert heard.understood is False and heard.presses == ()


# --- through the flow -----------------------------------------------------------


def test_one_sentence_answers_several_questions_in_whatever_order_it_was_said(
    monkeypatch,
):
    """ "For college, any budget, any brand" - said backwards, applied in order."""
    rig, listener = hearing_rig(
        monkeypatch,
        pressing(
            ("purpose", "study"),
            ("use", "college"),
            ("budget", "any"),
            ("brand", "any"),
        ),
    )
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    after = say(rig, view["session"])

    answered = [a["question"] for a in after["answered"]]
    assert answered == ["shelf", "brand", "budget", "purpose", "use"]
    assert after["question"]["id"] == "priority", "the suggestion is put, not skipped"
    assert after["heard"]["pressed"] == ["any", "any", "study", "college"]
    menu = listener.menus[0]
    assert {b["question"] for b in menu["now"]["buttons"]} == {"brand"}
    assert {"budget", "purpose", "use", "priority", "brand"} <= {
        a["question"] for a in menu["also"]
    }


def test_every_item_in_the_menu_carries_the_press_that_selects_it(monkeypatch):
    """Cards listed without a way to press them were heard and not pressed:
    "rendavathu phone kaatunga", transcribed perfectly, did nothing."""
    rig, listener = hearing_rig(monkeypatch, pressing(), pressing())
    from tests.test_guided_flow import NARROW

    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    rig.post("refine", session=view["session"], kind="list")
    rig.now[0] += 60
    say(rig, view["session"])
    rig.post("pick", session=view["session"], ref="1")
    rig.now[0] += 60
    say(rig, view["session"])

    on_results, on_card = listener.menus
    cards = [(c["question"], c["id"]) for c in on_results["cards"]]
    assert cards[:2] == [("pick", "1"), ("pick", "2")]
    offered = {(c["question"], c["id"]) for c in on_results["commands"]}
    assert {("command", "cheaper"), ("command", "next"), ("command", "list")} <= offered
    assert ("ask", "emi") in {
        (q["question"], q["id"]) for q in on_card["card_questions"]
    }


def test_the_second_one_picks_the_second_card(monkeypatch):
    rig, _ = hearing_rig(monkeypatch, pressing(("pick", "2")))
    from tests.test_guided_flow import NARROW

    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    result = rig.post("refine", session=view["session"], kind="list")
    rig.now[0] += 60

    card = say(rig, view["session"])

    assert (
        card["step"] == "card" and card["product"]["name"] == result["picks"][1]["name"]
    )
    assert card["heard"]["pressed"] == ["2"]


def test_asking_for_emi_out_loud_presses_the_emi_question(monkeypatch):
    from tests.test_guided_flow import OneAnswer
    from src.guided.answers import Answerer

    rig, _ = hearing_rig(monkeypatch, pressing(("ask", "emi")))
    rig.client.app.state.guided._answerer = Answerer(OneAnswer())  # type: ignore[attr-defined]
    from tests.test_guided_flow import NARROW

    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    rig.post("pick", session=view["session"], ref="1")
    rig.now[0] += 120

    answer = say(rig, view["session"])

    assert answer["step"] == "answer" and answer["heard"]["pressed"] == ["emi"]


def test_a_question_no_button_covers_goes_to_the_answerer_in_their_words(monkeypatch):
    from src.guided.answers import Answerer
    from tests.test_guided_flow import OneAnswer

    model = OneAnswer()
    rig, _ = hearing_rig(monkeypatch, pressing(question="கேமரா இரவுல நல்லா இருக்குமா?"))
    rig.client.app.state.guided._answerer = Answerer(model)  # type: ignore[attr-defined]
    from tests.test_guided_flow import NARROW

    view = rig.walk(*NARROW)
    rig.verify(view["session"])
    rig.post("recommend", session=view["session"])
    rig.post("pick", session=view["session"], ref="1")
    rig.now[0] += 120

    answer = say(rig, view["session"])

    assert answer["step"] == "answer" and model.asked == 1
    assert answer["left"] == 5, "a spoken question counts against the six"


def test_a_spoken_question_with_nothing_picked_is_not_answered(monkeypatch):
    from src.guided.answers import Answerer
    from tests.test_guided_flow import OneAnswer

    model = OneAnswer()
    rig, _ = hearing_rig(monkeypatch, pressing(question="EMI இருக்கா?"))
    rig.client.app.state.guided._answerer = Answerer(model)  # type: ignore[attr-defined]
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    heard = say(rig, view["session"])

    assert heard["step"] == "heard" and model.asked == 0
    assert heard["say"] == line("say_again")


@pytest.mark.parametrize(
    "press",
    [
        ("budget", "1-99999999"),
        ("brand", "Nokia"),
        ("pick", "9"),
        ("ask", "emi"),
        ("command", "rm -rf"),
    ],
)
def test_a_press_that_is_not_a_button_on_offer_does_nothing(monkeypatch, press):
    rig, _ = hearing_rig(monkeypatch, pressing(press))
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    heard = say(rig, view["session"])

    assert heard["step"] == "heard" and heard["heard"]["pressed"] == []
    assert heard["answered"] == view["answered"], "the screen stayed where it was"


def test_back_and_start_again_by_voice(monkeypatch):
    rig, _ = hearing_rig(
        monkeypatch, pressing(("command", "back")), pressing(("command", "restart"))
    )
    view = rig.walk(("shelf", "phone"), ("brand", "any"))
    rig.now[0] += 60

    assert say(rig, view["session"])["question"]["id"] == "brand"
    restarted = say(rig, view["session"])
    assert restarted["question"]["id"] == "shelf" and restarted["lang"] == "ta"


def test_a_hurried_speaker_is_asked_to_tap_and_the_ear_is_not_paid(monkeypatch):
    rig, listener = hearing_rig(monkeypatch, pressing(("brand", "any")))
    view = rig.walk(("shelf", "phone"))

    heard = say(rig, view["session"])

    assert heard["step"] == "heard" and heard["say"] == line("say_tap")
    assert listener.menus == []


def test_twenty_spoken_turns_then_taps(monkeypatch):
    # The per-client HTTP limit is the other line of defence, and it would
    # stop this test first: twenty-one requests in well under a minute.
    monkeypatch.setattr("src.web.routes.guided.HEARS_PER_MINUTE", 1_000)
    rig, listener = hearing_rig(monkeypatch, *[pressing() for _ in range(MOST_TURNS)])
    # The turn cap alone is under test here. With the shipped 25-paise
    # session cap the governor stops a paid ear after about six turns -
    # tested in its own right below - so it is given room to see the cap.
    from src.guided.sessions import Governor

    rig.client.app.state.guided._governor = Governor(  # type: ignore[attr-defined]
        usd_inr=89.0,
        gst_rate=0.18,
        target_inr_per_minute=1.50,
        ceiling_inr_per_minute=1.70,
        session_cap_inr=3.00,
        clock=lambda: rig.now[0],
    )
    view = rig.walk(("shelf", "phone"))

    for _ in range(MOST_TURNS):
        rig.now[0] += 120  # time on the line, so the governor is not the limit
        say(rig, view["session"])
    rig.now[0] += 120
    last = say(rig, view["session"])

    assert last["say"] == line("say_tap") and len(listener.menus) == MOST_TURNS
    assert last["mic"] is None, "the mic is not offered once the turns are spent"


@pytest.mark.parametrize("size", [LEAST_BYTES - 2, 0])
def test_a_clip_too_short_to_be_speech_is_not_sent(monkeypatch, size):
    rig, listener = hearing_rig(monkeypatch)
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    heard = say(rig, view["session"], b"\x00" * size)

    assert heard["say"] == line("say_again") and listener.menus == []


def test_a_body_longer_than_eight_seconds_is_refused_before_it_is_read(monkeypatch):
    rig, listener = hearing_rig(monkeypatch)
    view = rig.walk(("shelf", "phone"))

    response = rig.client.post(
        f"/api/v1/guided/hear?session={view['session']}",
        content=b"\x00" * (MOST_BYTES + 2),
    )

    assert response.status_code == 413 and listener.menus == []


@pytest.mark.parametrize("session", ["short", "../../etc/passwd/xx", "A" * 40])
def test_a_session_that_is_not_one_never_reaches_the_flow(monkeypatch, session):
    rig, listener = hearing_rig(monkeypatch)

    response = rig.client.post(f"/api/v1/guided/hear?session={session}", content=PCM)

    assert response.status_code == 422 and listener.menus == []


def test_the_mic_is_offered_only_when_there_is_an_ear(monkeypatch):
    rig, _ = hearing_rig(monkeypatch)
    assert rig.walk()["mic"] == {"label": "பேசுங்க"}

    plain = Rig(monkeypatch)
    assert plain.walk()["mic"] is None


def test_an_answer_said_ahead_of_its_question_is_remembered_and_pressed_when_it_comes(
    monkeypatch,
):
    """ "Engineering student" said while the brand is being asked: the budget
    still has to be asked, and when it is answered - by a tap - the purpose
    and the course are pressed without asking again."""
    rig, _ = hearing_rig(
        monkeypatch, pressing(("brand", "any"), ("purpose", "study"), ("use", "course"))
    )
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    heard = say(rig, view["session"])
    assert heard["question"]["id"] == "budget"
    assert heard["heard"]["pressed"] == ["any"]
    assert heard["heard"]["remembered"] == ["study", "course"]

    after = rig.post("answer", session=view["session"], question="budget", choice="any")
    answered = [a["question"] for a in after["answered"]]
    assert answered == ["shelf", "brand", "budget", "purpose", "use"]
    assert after["question"]["id"] == "priority"


def test_i_dont_mind_is_a_button_the_ear_can_press(monkeypatch):
    rig, listener = hearing_rig(monkeypatch, pressing())
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60
    say(rig, view["session"])

    also = {(a["question"], a["id"]) for a in listener.menus[0]["also"]}
    assert ("budget", "any") in also and ("brand", "any") in also


def test_going_back_forgets_what_was_said_ahead(monkeypatch):
    rig, _ = hearing_rig(monkeypatch, pressing(("brand", "any"), ("purpose", "gaming")))
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60
    say(rig, view["session"])

    rig.post("back", session=view["session"])
    after = rig.post("answer", session=view["session"], question="brand", choice="any")
    after = rig.post("answer", session=view["session"], question="budget", choice="any")

    assert after["question"]["id"] == "purpose", "a forgotten answer was pressed anyway"


def test_a_course_said_without_saying_studies_still_means_studies(monkeypatch):
    rig, _ = hearing_rig(
        monkeypatch, pressing(("brand", "any"), ("budget", "any"), ("use", "course"))
    )
    view = rig.walk(("shelf", "phone"))
    rig.now[0] += 60

    heard = say(rig, view["session"])

    answered = [(a["question"]) for a in heard["answered"]]
    assert answered == ["shelf", "brand", "budget", "purpose", "use"]
    assert heard["question"]["id"] == "priority"


def test_speaking_to_the_welcome_screen_answers_language_and_shelf_at_once(monkeypatch):
    """ "Enakku phone paakanum", said before anything was chosen: the Tamil
    it was said in presses ta, and the phone it asks for presses phone."""
    rig, listener = hearing_rig(
        monkeypatch, pressing(("language", "ta"), ("shelf", "phone"))
    )
    view = rig.post("start")
    rig.now[0] += 60

    heard = say(rig, view["session"])

    assert [a["question"] for a in heard["answered"]] == ["shelf"]
    assert heard["lang"] == "ta" and heard["question"]["id"] == "brand"
    menu = listener.menus[0]
    assert {b["id"] for b in menu["now"]["buttons"]} == {"ta", "en"}
    assert ("shelf", "phone") in {(a["question"], a["id"]) for a in menu["also"]}
    assert (
        "the language the shopper SPOKE"
        in __import__("src.guided.hear", fromlist=["x"]).INSTRUCTION
    )


def test_the_session_cap_stops_a_paid_ear_long_before_the_turn_cap(monkeypatch):
    """At the shipped 25 paise a call, the phone line's paid ear gets a
    handful of turns and then taps - the browser's free ear has no limit."""
    monkeypatch.setattr("src.web.routes.guided.HEARS_PER_MINUTE", 1_000)
    rig, listener = hearing_rig(monkeypatch, *[pressing() for _ in range(MOST_TURNS)])
    view = rig.walk(("shelf", "phone"))

    for _ in range(MOST_TURNS):
        rig.now[0] += 20
        last = say(rig, view["session"])

    assert 0 < len(listener.menus) < MOST_TURNS
    assert last["say"] == line("say_tap")
    assert last["meter"]["total_inr"] <= 0.25
