"""Shop words pressing buttons, by code alone - the zero-paisa ear.

Every sentence here is the kind the owner spoke in his walkthroughs,
in Tamil script, romanised Tamil and English, and not one of them may
reach a model before a product is picked.
"""

from __future__ import annotations


from src.guided.hear import Press
from src.guided.match import matches
from tests.test_guided_flow import NARROW, Rig


def heard(text: str, **state) -> list[Press]:
    base = dict(
        shelf=None, language_open=False, answered=frozenset(), shown=0, picked=False
    )
    base.update(state)
    return matches(text, **base)


def test_the_first_sentence_sets_language_shelf_brand_budget_and_purpose():
    """ "சாம்சங்ல முப்பதாயிரத்துக்குள்ள காலேஜ் படிக்கறதுக்கு ஒரு போன்" -
    one breath, five presses, zero paise."""
    presses = heard(
        "சாம்சங்ல முப்பதாயிரத்துக்குள்ள காலேஜ் படிக்கறதுக்கு ஒரு போன் வேணும்",
        language_open=True,
    )

    assert Press("language", "ta") in presses
    assert Press("shelf", "phone") in presses
    assert Press("brand", "samsung") in presses
    # "க்குள்ள" is a ceiling, kept to the rupee - not rounded to a band.
    assert Press("budget", "cap:30000") in presses
    assert Press("purpose", "study") in presses
    assert Press("use", "college") in presses


def test_english_is_heard_as_english():
    presses = heard("I need a phone under thirty thousand", language_open=True)

    assert Press("language", "en") in presses
    assert Press("shelf", "phone") in presses
    assert Press("budget", "cap:30000") in presses


def test_a_lakh_and_a_half_is_a_ceiling_of_one_and_a_half_lakhs():
    assert Press("budget", "cap:150000") in heard(
        "ஒன்றரை லட்சம் வரைக்கும் பரவாயில்ல", shelf="phone"
    )


def test_small_numbers_are_not_budgets():
    """ "2 போன் பாக்கணும்" is a count, not two rupees."""
    assert not any(p.question == "budget" for p in heard("2 போன் பாக்கணும்", shelf="phone"))


def test_any_words_press_the_any_buttons():
    presses = heard("எந்த பிராண்டும் பரவாயில்ல, பட்ஜெட் கவலை இல்ல", shelf="phone")

    assert Press("brand", "any") in presses and Press("budget", "any") in presses


def test_any_brand_says_nothing_about_the_budget():
    """The owner's own sentence. It pressed budget=any too, swallowed the
    budget said next, and the model was asked for want of a budget."""
    presses = heard("போன் வேணும் எந்த பிராண்டும் பரவாயில்ல", shelf="phone")

    assert Press("brand", "any") in presses
    assert not any(p.question == "budget" for p in presses)


def test_any_reaches_for_the_nearest_thing_it_is_about():
    presses = heard("சாம்சங் வேணும் விலை பரவாயில்ல", shelf="phone")
    assert Press("brand", "samsung") in presses and Press("budget", "any") in presses

    english = heard("any brand is fine", shelf="phone", language_open=False)
    assert Press("brand", "any") in english
    assert not any(p.question == "budget" for p in english)


def test_a_bare_any_answers_only_the_question_on_screen():
    on_budget = matches(
        "எதுவானாலும் பரவாயில்ல",
        shelf="phone",
        language_open=False,
        answered=frozenset(),
        shown=0,
        picked=False,
        current="budget",
    )
    assert [p for p in on_budget if p.choice == "any"] == [Press("budget", "any")]

    on_purpose = matches(
        "எதுவானாலும் பரவாயில்ல",
        shelf="phone",
        language_open=False,
        answered=frozenset(),
        shown=0,
        picked=False,
        current="purpose",
    )
    assert not any(p.choice == "any" for p in on_purpose)


def test_settled_questions_are_not_pressed_again():
    presses = heard(
        "முப்பதாயிரம் போதும், கேம் விளையாட",
        shelf="phone",
        answered=frozenset({"budget"}),
    )

    assert not any(p.question == "budget" for p in presses)
    assert Press("use", "gaming") in presses


def test_the_deep_chain_in_one_breath():
    presses = heard("நான் ஐடில டெவலப்பரா வேலை செய்றேன்", shelf="phone")

    assert Press("purpose", "work") in presses
    assert Press("use", "it") in presses
    assert Press("use2", "dev") in presses


def test_the_speaker_room_words_press_the_speaker_purposes():
    assert Press("purpose", "party") in heard(
        "பார்ட்டிக்கு வெளில எடுத்துட்டு போகணும்", shelf="speaker"
    )


def test_ordinals_pick_cards_and_the_last_one_is_the_last():
    assert Press("pick", "2") in heard("ரெண்டாவது போன காட்டுங்க", shelf="phone", shown=5)
    assert Press("pick", "5") in heard("கடைசி ஒண்ணு", shelf="phone", shown=5)
    assert not any(
        p.question == "pick" for p in heard("மூணாவது", shelf="phone", shown=2)
    ), "a card that is not on the list cannot be picked"


def test_card_words_press_the_card_questions():
    assert Press("ask", "emi") in heard(
        "இதுக்கு மாதம் எவ்வளவு கட்டணும்", shelf="phone", picked=True
    )
    assert Press("ask", "warranty") in heard(
        "warranty எவ்வளவு", shelf="phone", picked=True
    )
    assert Press("ask", "dealer") in heard(
        "உங்க ஆள்கிட்ட பேசணும்", shelf="phone", picked=True
    )


def test_commands_walk_the_suggestions():
    assert Press("command", "next") in heard("வேற ஒண்ணு காட்டுங்க", shelf="phone", shown=3)
    assert Press("command", "list") in heard("லிஸ்ட்டா அனுப்பு", shelf="phone", shown=3)
    assert Press("command", "cheaper") in heard(
        "கொஞ்சம் கம்மி விலைல", shelf="phone", shown=3
    )
    assert Press("command", "restart") in heard("முதல்ல இருந்து ஆரம்பிக்கலாம்", shelf="phone")


def test_a_bare_yes_defers_to_whatever_is_on_screen():
    assert heard("சரி") == [Press("__yes__", "__yes__")]


def test_nothing_matched_is_nothing_pressed():
    assert heard("வானம் நல்லா இருக்கு இன்னைக்கு") == []


# ── through the flow, as the browser would send it ──────────────────────────


def say_text(rig: Rig, session: str, text: str) -> dict:
    response = rig.client.post(
        "/api/v1/guided/said", json={"session": session, "text": text}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_one_spoken_sentence_walks_four_questions_for_nothing(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.post("start")

    after = say_text(
        rig, view["session"], "எனக்கு சாம்சங் போன், பட்ஜெட் பரவாயில்ல, வீட்டு பயன்பாட்டுக்கு"
    )

    answered = [a["question"] for a in after["answered"]]
    assert answered == ["shelf", "brand", "budget", "purpose"]
    assert after["lang"] == "ta"
    assert after["meter"]["total_inr"] == 0.0, "the owner's zero, held"
    assert rig.thinker.briefs == []


def test_sari_accepts_the_suggestion_on_screen(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.walk(*NARROW[:-1])  # standing on the budget_up question

    after = say_text(rig, view["session"], "சரி")

    assert after["step"] == "ready", "'சரி' pressed the first button, yes"


def test_unmatched_words_on_the_card_become_the_one_governed_question(monkeypatch):
    from src.guided.answers import Answerer
    from tests.test_guided_flow import OneAnswer

    model = OneAnswer()
    rig = Rig(monkeypatch, answerer=Answerer(model))
    view = rig.walk(*NARROW)
    rig.verify(view["session"])
    rig.post("recommend", session=view["session"])
    rig.post("pick", session=view["session"], ref="1")
    rig.now[0] += 120

    answer = say_text(rig, view["session"], "இது தண்ணில விழுந்தா கெட்டுப்போகுமா")

    assert answer["step"] == "answer" and model.asked == 1
    before_pick = Rig(monkeypatch)
    opening = before_pick.post("start")
    opening = say_text(before_pick, opening["session"], "தமிழ்")
    nothing = say_text(before_pick, opening["session"], "இது தண்ணில விழுந்தா கெட்டுப்போகுமா")
    assert nothing["step"] == "heard", "nothing matched, and that was said"
    assert nothing["meter"]["total_inr"] == 0.0, (
        "before a pick, unmatched words never reach a model"
    )


def test_a_spoken_brand_and_budget_that_clash_keep_the_budget_and_say_so(monkeypatch):
    """ "Samsung, under 15 thousand" when Samsung starts at 16,000: the
    owner's script keeps the budget, says the clash out loud, and asks
    the brand again - never silently drops what was said."""
    rig = Rig(monkeypatch)  # Samsung's cheapest is 16,000; Vivo holds 12,000
    view = rig.post("start")

    after = say_text(rig, view["session"], "சாம்சங் போன், பதினைஞ்சாயிரத்துக்குள்ள")

    # Not a dead end any more (owner's spec): the choice is put - another
    # brand at this budget, or Samsung from the next price it has.
    assert after["say"] == "இந்த பட்ஜெட்ல அந்த பிராண்ட் இல்ல. வேற பிராண்டா, அதே பிராண்டா?"
    answered = {a["question"]: a for a in after["answered"]}
    assert "brand" not in answered, "the dead brand was dropped"
    assert answered["budget"]["label"] == "₹15,000 வரை", "the budget held, exact"
    assert after["question"]["id"] == "clash"
    assert after["meter"]["total_inr"] == 0.0


def test_a_whole_call_by_voice_costs_nothing_and_asks_no_model(monkeypatch):
    """The owner asked for it confirmed: speaking costs ₹0. Every turn here
    is spoken - noise, a miss, the need in one breath, the read-back, a
    pick by ordinal, a card question - and the bill is zero, with the
    thinker, the answerer and the paid ear never reached."""
    from src.guided.answers import Answerer
    from tests.test_guided_flow import OneAnswer

    model = OneAnswer()
    rig = Rig(monkeypatch, answerer=Answerer(model))
    sid = rig.post("start")["session"]

    turns = [
        "தமிழ்",
        "வானம் நல்லா இருக்கு",  # nothing to press: a miss, still free
        "போன் வேணும், எந்த பிராண்டும் பரவாயில்ல",
        "இருபதாயிரம் முதல் முப்பதாயிரம் வரைக்கும்",
        "வீட்டு பயன்பாட்டுக்கு",
    ]
    view = {}
    for text in turns:
        rig.now[0] += 20
        view = say_text(rig, sid, text)
    while view.get("step") == "question":
        rig.now[0] += 20
        view = say_text(rig, sid, "சரி")  # accepts whatever is suggested
    assert view["step"] == "ready"

    rig.now[0] += 20
    result = rig.post("recommend", session=sid)
    assert result["step"] == "result"
    card = say_text(rig, sid, "முதல் ஒண்ணு")
    assert card["step"] == "card"
    asked = say_text(rig, sid, "இதுக்கு மாதம் எவ்வளவு கட்டணும்")

    assert asked["meter"]["total_inr"] == 0.0
    assert asked["meter"]["spoken_turns"] >= len(turns) + 3
    assert rig.thinker.briefs == [], "three in budget: nothing to judge"
    assert model.asked == 0, "EMI is read off the card, not asked of a model"


def test_a_brand_said_not_to_be_required_is_any_brand():
    """The owner's correction: "Samsung கட்டாயமில்லை, வேற பிராண்டும் ஓகே" used
    to press Samsung again - the brand was heard, the "not" was not."""
    presses = heard(
        "Samsung கட்டாயமில்லை, வேற பிராண்டும் ஓகே. ஆனா ₹25,000 தாண்டக் கூடாது",
        shelf="phone",
    )

    assert Press("brand", "any") in presses
    assert Press("brand", "samsung") not in presses
    assert Press("budget", "cap:25000") in presses
    assert Press("brand", "samsung") in heard("Samsung வேணும்", shelf="phone")


def test_the_correction_at_the_read_back_keeps_everything_else(monkeypatch):
    """Through the flow: brand loosened, budget and the rest kept, and the
    read-back comes again with the new summary."""
    rig = Rig(monkeypatch)
    view = rig.post("start")
    sid = view["session"]
    rig.post("answer", session=sid, question="language", choice="ta")
    say_text(rig, sid, "Samsung phone max ₹25,000 வீட்டு பயன்பாட்டுக்கு")
    view = say_text(rig, sid, "சரி")  # accept the suggested spec
    assert view["question"]["id"] == "confirm"

    corrected = say_text(
        rig, sid, "Samsung கட்டாயமில்லை, வேற பிராண்டும் ஓகே. ஆனா ₹25,000 தாண்டக் கூடாது"
    )
    labels = {a["question"]: a["label"] for a in corrected["answered"]}

    assert corrected["question"]["id"] == "confirm", "read back again"
    assert labels["brand"] == "எந்த பிராண்டும் பரவாயில்ல"
    assert labels["budget"] == "₹25,000 வரை"
    assert labels["purpose"] == "வீட்டு பயன்பாடு", "the purpose was kept"
    assert "Samsung" not in corrected["question"]["note"]


def test_a_card_letter_picks_the_card_on_screen_and_its_question_follows():
    presses = heard("C option-க்கு EMI இருக்கா?", shelf="phone", shown=4)
    assert presses[:2] == [Press("pick", "3"), Press("ask", "emi")]

    assert Press("pick", "1") in heard("option A பாக்கலாம்", shelf="phone", shown=4)
    assert Press("pick", "3") in heard("சி ஆப்ஷன் காட்டுங்க", shelf="phone", shown=4)
    assert not any(
        p.question == "pick" for p in heard("E option", shelf="phone", shown=4)
    ), "only cards that are on screen"
    assert not any(
        p.question == "pick" for p in heard("type c charger", shelf="phone", shown=4)
    ), "a bare letter is not a card"


def test_c_option_after_show_another_is_the_third_card_on_screen(monkeypatch):
    """After "வேற காட்டு" the screen holds 5-8: "C" is 7, not 3."""
    rig = Rig(monkeypatch, count=14)
    view = rig.walk(("shelf", "phone"), ("brand", "any"), ("budget", "any"))
    while view["step"] == "question":
        view = rig.post(
            "answer",
            session=view["session"],
            question=view["question"]["id"],
            choice=view["question"]["choices"][0]["id"],
        )
    rig.post("recommend", session=view["session"])
    second = rig.post("refine", session=view["session"], kind="next")
    third_on_screen = second["picks"][2]

    asked = say_text(rig, view["session"], "C option-க்கு EMI இருக்கா?")

    session = rig.sessions.get(view["session"])
    shown_codes = [p.item.item_code for p in session.shown]
    assert third_on_screen["ref"] == "7"
    assert session.picked == shown_codes[2]
    assert asked["step"] == "answer" and asked["heard"]["pressed"] == ["3", "emi"]


def test_picking_and_asking_in_one_breath_brings_the_card_with_the_answer(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])

    asked = say_text(rig, view["session"], "C option-க்கு EMI இருக்கா?")

    assert asked["step"] == "answer"
    assert asked["product"] is not None, "the card the answer is about comes too"
    assert asked["questions"]["choices"], "and its question buttons"


# ── the list the owner asked to complete, 2026-11-24 ────────────────────────


def test_around_a_figure_is_a_band_either_side():
    assert Press("budget", "range:22500-27500") in heard(
        "சுமார் ₹25,000 வரைக்கும்", shelf="phone"
    )
    assert Press("budget", "range:18000-22000") in heard("around 20000", shelf="phone")


def test_a_brand_can_be_left_out_or_preferred_not_only_chosen():
    left_out = heard("Samsung வேண்டாம், வேற எதுவும் ஓகே", shelf="phone")
    assert Press("exclude", "samsung") in left_out and Press("brand", "any") in left_out
    assert Press("exclude", "samsung") in heard("no Samsung please", shelf="phone")
    assert Press("exclude", "samsung") not in heard(
        "no idea really, Samsung is fine", shelf="phone"
    )
    preferred = heard("Vivo இருந்தா நல்லது", shelf="phone")
    assert Press("prefer", "vivo") in preferred and Press("brand", "any") in preferred


def test_features_are_heard_as_requirements():
    assert Press("feature", "usb_c") in heard("USB-C வேணும்", shelf="phone")
    assert Press("feature", "five_g") in heard("5G phone venum", shelf="phone")
    assert Press("feature", "amoled") in heard("AMOLED screen", shelf="phone")
    assert not any(p.question == "feature" for p in heard("USB-C", shelf="speaker"))


def test_a_left_out_brand_is_gone_and_said_back(monkeypatch):
    rig = Rig(monkeypatch)
    sid = rig.post("start")["session"]
    rig.post("answer", session=sid, question="language", choice="ta")

    view = say_text(rig, sid, "போன் வேணும், Samsung வேண்டாம்")

    assert view["brief"]["must"]["not_brands"] == ["Samsung"]
    budget_left = sum(
        c["count"] or 0 for c in view["question"]["choices"] if c["id"] != "any"
    )
    assert budget_left > 0
    session = rig.sessions.get(sid)
    from src.guided import engine
    from src.guided.script import BY_SHELF

    items = rig.client.app.state.guided._book.held("phone")
    left = engine.matching(BY_SHELF["phone"], items, session.answers)
    assert left and all((i.brand or "").lower() != "samsung" for i in left)


def test_a_preferred_brand_comes_first_and_nothing_is_hidden(monkeypatch):
    rig = Rig(monkeypatch)
    view = rig.walk(("shelf", "phone"), ("brand", "any"), ("budget", "any"))
    sid = view["session"]
    say_text(rig, sid, "Vivo இருந்தா நல்லது")
    view = rig.post("answer", session=sid, question="purpose", choice="family")
    while view["step"] == "question":
        view = rig.post(
            "answer",
            session=sid,
            question=view["question"]["id"],
            choice=view["question"]["choices"][0]["id"],
        )

    result = rig.post("recommend", session=sid)
    listed = rig.post("refine", session=sid, kind="list")

    brands = [p["brand"] for p in listed["picks"]]
    vivos = [i for i, b in enumerate(brands) if b == "Vivo"]
    assert vivos and vivos == list(range(len(vivos))), "every Vivo first"
    assert set(brands) - {"Vivo"}, "and the others still shown"
    assert result["brief"]["prefer"]["brands"] == ["Vivo"]


def test_a_brand_with_nothing_in_budget_offers_its_next_price(monkeypatch):
    """Samsung starts at 16,000 here: "Samsung under 15,000" is asked
    "another brand, or Samsung from its next price?" - with the figure."""
    rig = Rig(monkeypatch)
    sid = rig.post("start")["session"]
    rig.post("answer", session=sid, question="language", choice="ta")

    asked = say_text(rig, sid, "Samsung phone under 15000")

    assert asked["question"]["id"] == "clash"
    assert [c["id"] for c in asked["question"]["choices"]] == ["other", "same"]
    assert "Samsung" in asked["question"]["choices"][1]["label"]
    assert "₹16,000" in asked["question"]["note"]

    same = rig.post("answer", session=sid, question="clash", choice="same")
    labels = {a["question"]: a["label"] for a in same["answered"]}
    assert labels["brand"] == "Samsung" and labels["budget"] != "₹15,000 வரை"


def test_choosing_another_brand_keeps_the_budget(monkeypatch):
    rig = Rig(monkeypatch)
    sid = rig.post("start")["session"]
    rig.post("answer", session=sid, question="language", choice="ta")
    say_text(rig, sid, "Samsung phone under 15000")

    other = rig.post("answer", session=sid, question="clash", choice="other")

    labels = {a["question"]: a["label"] for a in other["answered"]}
    assert labels.get("budget") == "₹15,000 வரை", "the budget they said is kept"
    assert labels.get("brand") != "Samsung"


def test_when_the_cards_run_out_the_call_asks_what_to_change(monkeypatch):
    rig = Rig(monkeypatch, count=6)
    view = rig.walk(*NARROW)
    sid = view["session"]
    rig.post("recommend", session=sid)

    ran_out = {}
    for _ in range(3):
        ran_out = rig.post("refine", session=sid, kind="next")
    assert ran_out["relax"], "one change that finds more, with a count"
    budget = next(o for o in ran_out["relax"] if o["id"].startswith("budget:"))

    relaxed = rig.post("answer", session=sid, question="relax", choice=budget["id"])
    assert relaxed["step"] in ("ready", "question")
    assert any(a["question"] == "budget" for a in relaxed["answered"])
