"""Phase 1: type on the Guided screen, hear what was understood, get the
next question. The two examples are the owner's own, word for word.

Every turn here goes through HTTP and the free `/said` path, so the bill is
asserted too: typing costs what tapping costs - nothing.
"""

from __future__ import annotations

from pathlib import Path

from src.guided import engine
from src.guided.engine import Answer
from src.guided.script import BY_SHELF, LINES
from tests.test_guided_flow import Rig

PHONES = BY_SHELF["phone"]


def typed(rig: Rig, session: str, text: str) -> dict:
    response = rig.client.post(
        "/api/v1/guided/said",
        json={"session": session, "text": text, "kind": "text"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def tamil_session(rig: Rig) -> str:
    view = rig.post("start")
    rig.post("answer", session=view["session"], question="language", choice="ta")
    return view["session"]


def test_a_samsung_phone_is_said_back_and_the_budget_is_asked(monkeypatch):
    rig = Rig(monkeypatch)
    sid = tamil_session(rig)

    view = typed(rig, sid, "I need a Samsung phone.")

    assert view["overview"]["text"] == "சரிங்க, Samsung போன் பாக்குறீங்க."
    assert view["question"]["id"] == "budget"
    assert view["question"]["ask"] == "அதிகபட்சம் எவ்வளவு வரை பார்க்கலாம்?"
    assert view["lang"] == "ta", "typed English does not switch a Tamil call"
    assert view["meter"]["total_inr"] == 0.0 and view["meter"]["typed_turns"] == 1


def test_brand_budget_and_site_video_in_one_line_go_straight_to_movement(monkeypatch):
    rig = Rig(monkeypatch)  # Samsung holds phones at and under 25,000 here
    sid = tamil_session(rig)

    view = typed(
        rig,
        sid,
        "I need a Samsung phone, max ₹25,000. I take videos at construction sites.",
    )

    assert view["overview"]["text"] == (
        "சரிங்க, சைட்ல வீடியோ எடுக்க ₹25,000க்குள் Samsung போன் பாக்குறீங்க."
    )
    assert view["question"]["id"] == "use2"
    assert view["question"]["ask"] == (
        "நடந்து கொண்டே வீடியோ எடுப்பீங்களா, ஒரே இடத்திலிருந்து எடுப்பீங்களா?"
    )
    assert [c["id"] for c in view["question"]["choices"]] == ["moving", "still"]
    assert view["meter"]["total_inr"] == 0.0 and rig.thinker.briefs == []


def test_the_movement_answer_changes_what_is_suggested(monkeypatch):
    """Walking points at stabilisation, a fixed spot at the camera - the
    question is asked because its answer moves the ranking."""
    rig = Rig(monkeypatch)
    sid = tamil_session(rig)
    typed(
        rig,
        sid,
        "I need a Samsung phone, max ₹25,000. I take videos at construction sites.",
    )

    walking = typed(rig, sid, "நடந்துகிட்டே தான் எடுப்பேன்")

    assert walking["question"]["id"] == "priority"
    assert walking["question"]["choices"][0]["id"] == "steady"
    assert "spec-ல உறுதியா இருக்கறது மட்டும்" in walking["question"]["note"]


def test_a_turn_that_answers_nothing_about_the_need_has_no_overview(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.post("start")

    language_only = typed(rig, view["session"], "தமிழ்")
    missed = typed(rig, view["session"], "வானம் நல்லா இருக்கு")

    assert "overview" not in language_only
    assert missed["step"] == "heard" and "overview" not in missed


def test_the_overview_never_names_a_brand_that_was_dropped(monkeypatch):
    """Samsung starts at 16,000 here: under 15,000 it is dropped, and the
    overview must not still say "Samsung"."""
    rig = Rig(monkeypatch)
    sid = tamil_session(rig)

    view = typed(rig, sid, "Samsung phone under 15000")

    assert "Samsung" not in view["overview"]["text"]
    assert "₹15,000க்குள்" in view["overview"]["text"]


def test_the_overview_in_english():
    answers = (
        Answer("brand", "samsung", "Samsung"),
        Answer("budget", "cap:25000", "Up to ₹25,000"),
        Answer("purpose", "work", "For work"),
        Answer("use", "site_video", "Photos and video on site"),
    )
    assert engine.overview(PHONES, answers, "en") == (
        "Sure, you're looking for a Samsung phone up to ₹25,000 to shoot video on site."
    )


def test_budget_phrases_follow_what_was_said():
    phrase = engine._budget_phrase
    assert phrase(PHONES, "cap:25000", "ta") == "₹25,000க்குள்"
    assert phrase(PHONES, "floor:50000", "ta") == "₹50,000க்கு மேல"
    assert phrase(PHONES, "range:20000-30000", "en") == "between ₹20,000 and ₹30,000"
    assert phrase(PHONES, "any", "ta") == ""


def test_the_composer_is_on_the_guided_screen_from_the_first_second():
    """Box, send and mic inside the Guided panel, none of them hidden."""
    page = Path("src/web/static/index.html").read_text()
    panel = page[page.index('id="guided-panel"') : page.index('id="voice-panel"')]
    for element in ('id="g-compose"', 'id="g-text"', 'id="g-send"', 'id="g-mic"'):
        assert element in panel, element
        tag = panel[
            panel.rindex("<", 0, panel.index(element)) : panel.index(
                ">", panel.index(element)
            )
        ]
        assert " hidden" not in tag, f"{element} starts hidden"


def test_every_new_line_is_in_both_languages():
    for key in ("ask_budget", "ask_movement", "say_ack"):
        tamil, english = LINES[key]
        assert tamil and english, key


def test_any_brand_is_ok_is_any_brand_not_the_first_brand(monkeypatch):
    """Live, 2026-11-24: "எந்த பிராண்டும் ஓகே" pressed Nokia - "ஓகே" was read
    as a bare yes, and a yes pressed the first button."""
    rig = Rig(monkeypatch)
    sid = tamil_session(rig)
    typed(rig, sid, "போன் வேணும்")

    view = typed(rig, sid, "எந்த பிராண்டும் ஓகே")
    brand = next(a for a in view["answered"] if a["question"] == "brand")

    assert brand["label"] == "எந்த பிராண்டும் பரவாயில்ல"


def test_a_bare_yes_on_the_brand_question_presses_nothing(monkeypatch):
    rig = Rig(monkeypatch)
    sid = tamil_session(rig)
    typed(rig, sid, "போன் வேணும்")

    view = typed(rig, sid, "சரி")

    assert view["step"] == "heard"
    assert not any(a["question"] == "brand" for a in view["answered"])


# ── the Voice Call tab is the guided voice now (owner, 2026-11-24) ──────────


def test_paid_live_voice_is_off_unless_a_deploy_turns_it_on(monkeypatch):
    """Paid voice stays off. LIVE_VOICE=1 is the explicit opt-in."""
    monkeypatch.delenv("LIVE_VOICE", raising=False)
    rig = Rig(monkeypatch)

    assert rig.client.get("/api/v1/config").json()["live_voice"] is False


def test_live_voice_off_refuses_the_paid_socket(monkeypatch):
    """LIVE_VOICE=0: the socket refuses at the handshake, so no page, old tab
    or script can open a paid call, and the tab opens the guided voice."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("LIVE_VOICE", "0")
    rig = Rig(monkeypatch)

    assert rig.client.get("/api/v1/config").json()["live_voice"] is False
    with pytest.raises(WebSocketDisconnect) as refused:
        with rig.client.websocket_connect("/ws"):
            pass
    assert refused.value.code == 1008


def test_saying_english_into_a_tamil_recogniser_still_means_english(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.post("start")

    said = rig.client.post(
        "/api/v1/guided/said", json={"session": view["session"], "text": "இங்கிலீஷ்"}
    ).json()

    assert said["lang"] == "en"


def test_the_voice_call_tab_starts_the_guided_voice():
    """The page wiring, checked as text: the tab's button falls through to
    the guided voice whenever the server says Live is off."""
    app = Path("src/web/static/app.js").read_text()
    guided = Path("src/web/static/guided.js").read_text()
    assert "!state.config?.live_voice" in app and "startGuidedVoice()" in app
    assert "function startGuidedVoice()" in guided
    assert "showTab('guided', true)" in guided


def test_a_sentence_that_chooses_english_is_said_back_in_english(monkeypatch):
    """Live, 2026-11-25: "i need samsung phone under 20K" typed on the first
    screen chose English, and the overview came back in Tamil - its language
    was read before the sentence chose one."""
    rig = Rig(monkeypatch)
    sid = rig.post("start")["session"]

    view = typed(rig, sid, "i need samsung phone under 20K")

    assert view["lang"] == "en"
    assert view["overview"]["text"] == (
        "Sure, you're looking for a Samsung phone up to ₹20,000."
    )
    assert view["question"]["ask"] == "What will you mainly use it for?"


def test_a_forgotten_session_is_a_404_the_page_can_recover_from(monkeypatch):
    """With no database, a restart forgets every session. The page starts a
    new one and sends the same words again on this 404 (guided.js)."""
    rig = Rig(monkeypatch)

    response = rig.client.post(
        "/api/v1/guided/said",
        json={"session": "AAAAAAAAAAAAAAAAAAAAAA", "text": "i need samsung phone"},
    )

    assert response.status_code == 404
    page = Path("src/web/static/guided.js").read_text()
    assert (
        "if (forgotten && !again)" in page
        and "return sendSaid(text, kind, true)" in page
    )
