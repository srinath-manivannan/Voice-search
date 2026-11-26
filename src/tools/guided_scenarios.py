"""Fifty retail conversations, run against a live Guided server.

    .venv/bin/python -m src.tools.guided_scenarios [http://127.0.0.1:8080]

The release gate the owner's spec asks for: twenty-five Tamil and twenty-five
English journeys, each typed the way a customer would, each checked for what
a customer would notice - the right next question, an honest overview, a
budget respected to the rupee, a brand left out when they said so, and a
bill of nothing. Checks are about properties, not particular phones, so the
gate holds while stock and prices move.

Every turn goes through the same free path as speech (`/said`), so a pass
here also says no model was reached before a recommendation.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

View = dict[str, Any]
Check = Callable[[list[View]], str | None]


@dataclass
class Scenario:
    name: str
    lang: str
    turns: list[str]
    checks: list[Check] = field(default_factory=list)


# ── checks: each returns None when it holds, or what went wrong ─────────────


def overview_has(*parts: str) -> Check:
    def check(views: list[View]) -> str | None:
        said = " ".join((v.get("overview") or {}).get("text", "") for v in views)
        missing = [p for p in parts if p not in said]
        return f"overview lacks {missing}: {said!r}" if missing else None

    return check


def asks(*questions: str) -> Check:
    def check(views: list[View]) -> str | None:
        got = (views[-1].get("question") or {}).get("id")
        return None if got in questions else f"asked {got!r}, wanted {questions}"

    return check


def step_is(*steps: str) -> Check:
    def check(views: list[View]) -> str | None:
        got = views[-1].get("step")
        return None if got in steps else f"step {got!r}, wanted {steps}"

    return check


def cards_within(low: int | None, high: int | None) -> Check:
    def check(views: list[View]) -> str | None:
        picks: list[View] = _last_picks(views)
        if not picks:
            return "no cards were shown"
        prices = [p["price"] for p in picks if not p.get("over_budget")]
        bad = [
            x for x in prices if (low and x < low) or (high is not None and x > high)
        ]
        return f"cards outside the budget: {bad}" if bad else None

    return check


def no_brand(brand: str) -> Check:
    def check(views: list[View]) -> str | None:
        picks: list[View] = _last_picks(views)
        bad = [p["name"] for p in picks if (p.get("brand") or "").lower() == brand]
        return f"{brand} shown though left out: {bad}" if bad else None

    return check


def only_brand(brand: str) -> Check:
    def check(views: list[View]) -> str | None:
        picks: list[View] = _last_picks(views)
        bad = [p["name"] for p in picks if (p.get("brand") or "").lower() != brand]
        return f"other brands shown: {bad}" if bad else None

    return check


def no_keypad_over(rupees: int) -> Check:
    def check(views: list[View]) -> str | None:
        picks: list[View] = _last_picks(views)
        cheap = [p["name"] for p in picks if p["price"] < rupees]
        return f"keypad-priced phones shown: {cheap}" if cheap else None

    return check


def brief_must(key: str) -> Check:
    def check(views: list[View]) -> str | None:
        must = ((views[-1].get("brief") or {}).get("must")) or {}
        return None if key in must else f"brief.must lacks {key!r}: {must}"

    return check


def says(*parts: str) -> Check:
    def check(views: list[View]) -> str | None:
        said = views[-1].get("say") or ""
        missing = [p for p in parts if p not in said]
        return f"said {said!r}, lacks {missing}" if missing else None

    return check


def in_language(lang: str) -> Check:
    def check(views: list[View]) -> str | None:
        got = views[-1].get("lang")
        return None if got == lang else f"language {got!r}, wanted {lang!r}"

    return check


def free(views: list[View]) -> str | None:
    meter = views[-1].get("meter") or {}
    if meter.get("total_inr", 0) or meter.get("thinks", 0):
        return f"spent ₹{meter.get('total_inr')} with {meter.get('thinks')} thinks"
    return None


def _last_picks(views: list[View]) -> list[View]:
    """The cards on the last screen that had any."""
    for view in reversed(views):
        if view.get("picks"):
            return list(view["picks"])
    return []


# ── the fifty ───────────────────────────────────────────────────────────────

PHONE_TO_RESULTS = "@through"  # answer the first button until the cards


def _tamil() -> list[Scenario]:
    return [
        Scenario(
            "ta01 brand only",
            "ta",
            ["I need a Samsung phone."],
            [overview_has("Samsung போன்"), asks("budget"), free],
        ),
        Scenario(
            "ta02 brand budget site video",
            "ta",
            [
                "I need a Samsung phone, max ₹25,000. I take videos at construction sites."
            ],
            [overview_has("சைட்ல வீடியோ", "₹25,000க்குள்", "Samsung"), asks("use2"), free],
        ),
        Scenario(
            "ta03 tamil sentence",
            "ta",
            ["சாம்சங் போன் முப்பதாயிரத்துக்குள்ள வேணும்"],
            [overview_has("₹30,000க்குள்"), free],
        ),
        Scenario(
            "ta04 around a figure",
            "ta",
            ["போன் சுமார் ₹25,000"],
            [overview_has("₹22,500 – ₹27,500"), free],
        ),
        Scenario(
            "ta05 leave a brand out",
            "ta",
            ["போன் வேணும், Samsung வேண்டாம், ₹30,000 வரை", PHONE_TO_RESULTS],
            [no_brand("samsung"), cards_within(None, 30000), free],
        ),
        Scenario(
            "ta06 prefer a brand",
            "ta",
            ["போன், ₹25,000 வரை, Vivo இருந்தா நல்லது", PHONE_TO_RESULTS],
            [cards_within(None, 25000), free],
        ),
        Scenario(
            "ta07 usb-c",
            "ta",
            ["போன் ₹25,000 வரை USB-C வேணும்", PHONE_TO_RESULTS],
            [brief_must("features"), cards_within(None, 25000), free],
        ),
        Scenario(
            "ta08 5g",
            "ta",
            ["5G போன் ₹20,000 வரை", PHONE_TO_RESULTS],
            [brief_must("features"), cards_within(None, 20000), free],
        ),
        Scenario(
            "ta09 no keypads for a smartphone need",
            "ta",
            ["போன் ₹25,000 வரை, வீடியோ எடுக்க", PHONE_TO_RESULTS],
            [no_keypad_over(5000), free],
        ),
        Scenario(
            "ta10 keypads for a tiny budget",
            "ta",
            ["போன் ₹3,000 வரை"],
            [overview_has("₹3,000க்குள்"), free],
        ),
        Scenario(
            "ta11 brand with nothing in budget",
            "ta",
            ["Apple phone ₹10,000 வரை"],
            [asks("clash", "brand", "purpose"), free],
        ),
        Scenario(
            "ta12 off-button brand",
            "ta",
            ["Apple phone வேணும்"],
            [asks("budget"), overview_has("Apple"), free],
        ),
        Scenario(
            "ta13 correction at the read-back",
            "ta",
            [
                "Samsung phone ₹25,000 வரை வீட்டு பயன்பாட்டுக்கு",
                "சரி",
                "Samsung கட்டாயமில்லை, வேற பிராண்டும் ஓகே. ஆனா ₹25,000 தாண்டக் கூடாது",
            ],
            [asks("confirm"), free],
        ),
        Scenario(
            "ta14 four then next four",
            "ta",
            [
                "போன் எந்த பிராண்டும் பரவாயில்ல பட்ஜெட் கவலை இல்ல ஜாலிக்கு",
                PHONE_TO_RESULTS,
                "இதுல எதுவும் வேண்டாம், வேற காட்டு",
            ],
            [step_is("result"), free],
        ),
        Scenario(
            "ta15 list of ten",
            "ta",
            [
                "போன் எந்த பிராண்டும் பரவாயில்ல பட்ஜெட் கவலை இல்ல ஜாலிக்கு",
                PHONE_TO_RESULTS,
                "பத்து லிஸ்ட்டா காட்டு",
            ],
            [step_is("result"), free],
        ),
        Scenario(
            "ta16 C option EMI",
            "ta",
            [
                "Samsung phone ₹25,000 வரை வீட்டு பயன்பாட்டுக்கு",
                PHONE_TO_RESULTS,
                "C option-க்கு EMI இருக்கா?",
            ],
            [step_is("answer"), free],
        ),
        Scenario(
            "ta17 dealer by voice",
            "ta",
            [
                "Vivo phone வீட்டு பயன்பாட்டுக்கு",
                PHONE_TO_RESULTS,
                "முதல் ஒண்ணு",
                "டீலர்கிட்ட பேசணும்",
            ],
            [step_is("dealer"), free],
        ),
        Scenario(
            "ta18 engineering student",
            "ta",
            ["போன் எந்த பிராண்டும் பரவாயில்ல பட்ஜெட் கவலை இல்ல இன்ஜினியரிங் படிக்கிறேன்"],
            [asks("priority"), free],
        ),
        Scenario(
            "ta19 tv for the hall",
            "ta",
            ["டிவி வேணும் ஹாலுக்கு ₹40,000 வரை"],
            [overview_has("டிவி", "₹40,000க்குள்"), free],
        ),
        Scenario(
            "ta20 fridge",
            "ta",
            ["ஃப்ரிட்ஜ் வேணும் ₹30,000 வரை", PHONE_TO_RESULTS],
            [cards_within(None, 30000), free],
        ),
        Scenario(
            "ta21 ac",
            "ta",
            ["ஏசி வேணும் ₹45,000 வரை"],
            [overview_has("₹45,000க்குள்"), free],
        ),
        Scenario(
            "ta22 speaker for a party",
            "ta",
            ["ஸ்பீக்கர் வேணும் பார்ட்டிக்கு", PHONE_TO_RESULTS],
            [step_is("result"), free],
        ),
        Scenario(
            "ta23 headphones",
            "ta",
            ["ஹெட்ஃபோன் வேணும் ₹3,000 வரை"],
            [overview_has("₹3,000க்குள்"), free],
        ),
        Scenario(
            "ta24 nothing understood",
            "ta",
            ["வானம் நல்லா இருக்கு இன்னைக்கு"],
            [step_is("heard"), free],
        ),
        Scenario(
            "ta25 site video walking",
            "ta",
            [
                "I need a Samsung phone, max ₹25,000. I take videos at construction sites.",
                "நடந்துகிட்டே",
            ],
            [asks("priority"), free],
        ),
    ]


def _english() -> list[Scenario]:
    return [
        Scenario(
            "en01 brand only",
            "en",
            ["I need a Samsung phone."],
            [overview_has("Samsung phone"), asks("budget"), in_language("en"), free],
        ),
        Scenario(
            "en02 budget for mother",
            "en",
            ["I need a phone under 20000 for my mother"],
            [overview_has("₹20,000"), free],
        ),
        Scenario(
            "en03 around",
            "en",
            ["a phone around 30000"],
            [overview_has("₹27,000", "₹33,000"), free],
        ),
        Scenario(
            "en04 above",
            "en",
            ["a phone above 50000"],
            [overview_has("above ₹50,000"), free],
        ),
        Scenario(
            "en05 between",
            "en",
            ["a phone between 15000 and 20000", PHONE_TO_RESULTS],
            [cards_within(15000, 20000), free],
        ),
        Scenario(
            "en06 exclude",
            "en",
            ["a phone up to 30000, no Samsung please", PHONE_TO_RESULTS],
            [no_brand("samsung"), free],
        ),
        Scenario(
            "en07 prefer",
            "en",
            ["a phone up to 25000, preferably Vivo", PHONE_TO_RESULTS],
            [cards_within(None, 25000), free],
        ),
        Scenario(
            "en08 usb-c",
            "en",
            ["a phone up to 25000 with type c", PHONE_TO_RESULTS],
            [brief_must("features"), free],
        ),
        Scenario(
            "en09 amoled",
            "en",
            ["a phone up to 30000 with an AMOLED screen", PHONE_TO_RESULTS],
            [brief_must("features"), free],
        ),
        Scenario(
            "en10 brand and budget kept",
            "en",
            ["Vivo phone under 30000", PHONE_TO_RESULTS],
            [only_brand("vivo"), cards_within(None, 30000), free],
        ),
        Scenario(
            "en11 clash",
            "en",
            ["Apple phone under 10000"],
            [asks("clash", "brand", "purpose"), free],
        ),
        Scenario(
            "en12 off-button brand",
            "en",
            ["I want an Apple phone"],
            [overview_has("Apple"), free],
        ),
        Scenario(
            "en13 gaming",
            "en",
            ["a phone for gaming under 30000"],
            [overview_has("₹30,000"), free],
        ),
        Scenario(
            "en14 photos",
            "en",
            ["a phone for photos and reels", PHONE_TO_RESULTS],
            [step_is("result"), free],
        ),
        Scenario(
            "en15 office work",
            "en",
            ["a phone for office work under 25000"],
            [overview_has("₹25,000"), free],
        ),
        Scenario(
            "en16 next four",
            "en",
            ["any phone, any budget, for fun", PHONE_TO_RESULTS, "show me another"],
            [step_is("result"), free],
        ),
        Scenario(
            "en17 list",
            "en",
            ["any phone, any budget, for fun", PHONE_TO_RESULTS, "show me the list"],
            [step_is("result"), free],
        ),
        Scenario(
            "en18 option b",
            "en",
            ["Vivo phone for family", PHONE_TO_RESULTS, "option B, what is the EMI"],
            [step_is("answer"), free],
        ),
        Scenario(
            "en19 laptop",
            "en",
            ["a laptop under 60000"],
            [overview_has("₹60,000"), free],
        ),
        Scenario(
            "en20 tv",
            "en",
            ["a TV under 30000", PHONE_TO_RESULTS],
            [cards_within(None, 30000), free],
        ),
        Scenario(
            "en21 washing machine",
            "en",
            ["a washing machine under 25000"],
            [overview_has("₹25,000"), free],
        ),
        Scenario(
            "en22 watch",
            "en",
            ["a smartwatch under 5000"],
            [overview_has("₹5,000"), free],
        ),
        Scenario(
            "en23 speaker",
            "en",
            ["a bluetooth speaker for my room", PHONE_TO_RESULTS],
            [step_is("result"), free],
        ),
        Scenario(
            "en24 gibberish", "en", ["the sky is blue today"], [step_is("heard"), free]
        ),
        Scenario(
            "en25 english stays english",
            "en",
            ["phone for my dad under 15000"],
            [in_language("en"), overview_has("₹15,000"), free],
        ),
    ]


SCENARIOS = _tamil() + _english()


# ── running them ────────────────────────────────────────────────────────────


class Runner:
    def __init__(self, base: str) -> None:
        self._base = base.rstrip("/") + "/api/v1/guided/"
        self._client = httpx.Client(timeout=120, trust_env=False)

    def post(self, path: str, **body: Any) -> View:
        # The server limits new sessions per client per minute - the same
        # guard a real abuser meets. The runner waits its turn like a person.
        for _ in range(20):
            response = self._client.post(
                self._base + path,
                json=body,
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            if response.status_code != 429:
                break
            time.sleep(5)
        response.raise_for_status()
        return dict(response.json())

    def run(self, scenario: Scenario) -> list[str]:
        sid = self.post("start")["session"]
        self.post("answer", session=sid, question="language", choice=scenario.lang)
        views: list[View] = []
        for turn in scenario.turns:
            if turn == PHONE_TO_RESULTS:
                view = views[-1] if views else self.post("back", session=sid)
                for _ in range(10):
                    if view.get("step") != "question":
                        break
                    q = view["question"]
                    view = self.post(
                        "answer",
                        session=sid,
                        question=q["id"],
                        choice=q["choices"][0]["id"],
                    )
                    views.append(view)
                if view.get("step") == "ready":
                    views.append(self.post("recommend", session=sid))
                continue
            views.append(self.post("said", session=sid, text=turn, kind="text"))
        return [problem for check in scenario.checks if (problem := check(views))]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    runner = Runner(base)
    failed: dict[str, list[str]] = {}
    for scenario in SCENARIOS:
        try:
            problems = runner.run(scenario)
        except httpx.HTTPError as error:
            problems = [f"request failed: {error}"]
        mark = "PASS" if not problems else "FAIL"
        print(f"{mark} {scenario.name}" + (f"  -> {problems}" if problems else ""))
        if problems:
            failed[scenario.name] = problems
    passed = len(SCENARIOS) - len(failed)
    print(f"\n{passed}/{len(SCENARIOS)} passed")
    print(
        json.dumps(
            {"passed": passed, "total": len(SCENARIOS), "failed": failed},
            ensure_ascii=False,
        )
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
