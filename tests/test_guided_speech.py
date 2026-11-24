"""What the guided call says from live data: built by code, heard by us first.

The voice and the ear are fakes. What is tested is the rule that matters:
a sentence with a price in it is played only if the price was heard as
written - and everything around it that decides what is paid for.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.guided import spoken
from src.guided.model import ModelUnavailable
from src.guided.speech import (
    MOST_CHARS,
    SpeechBook,
    heard_as_said,
    tts_rates,
    worst_case_usd,
)


def test_the_voice_costs_twice_as_much_from_the_first_of_january():
    assert tts_rates(date(2026, 12, 31)).audio_out == 6.00
    assert tts_rates(date(2027, 1, 1)).audio_out == 12.00
    assert worst_case_usd(date(2027, 1, 1)) > worst_case_usd(date(2026, 12, 31))
    # The most one sentence can cost, in rupees with GST: under a rupee.
    assert worst_case_usd(date(2027, 1, 1)) * 89 * 1.18 < 1.20


# --- the sentences -------------------------------------------------------------


PICKS = [
    {
        "name": "Poco M8 5G ( Carbon Black, 6GB-128GB )",
        "price": 24_999,
        "over_budget": None,
    },
    {
        "name": "Realme P4 Lite 5G ( Mosaic Blue, 4GB-128GB )",
        "price": 20_999,
        "over_budget": None,
    },
    {
        "name": "Samsung Galaxy A17 5G W/O TA ( Gray, 8GB-128GB )",
        "price": 32_999,
        "over_budget": "உங்க பட்ஜெட்டை விட கொஞ்சம் அதிகம்",
    },
    {"name": "Fourth Phone", "price": 9_999, "over_budget": None},
]


def test_a_name_is_said_without_the_colour_and_memory_in_brackets():
    assert spoken.spoken_name("Poco M8 5G ( Carbon Black, 6GB-128GB )") == "Poco M8 5G"
    assert (
        spoken.spoken_name("Samsung Galaxy A17 5G Blue (8GB | 256GB)")
        == "Samsung Galaxy A17 5G Blue"
    )


def test_three_are_read_out_with_their_prices_and_the_one_over_budget_says_so():
    said = spoken.results(PICKS, "ta")

    assert said is not None
    assert said.text.startswith("ஒண்ணு: Poco M8 5G, 24,999 ரூபாய்.")
    assert "ரெண்டு: Realme P4 Lite 5G, 20,999 ரூபாய்." in said.text
    assert (
        "மூணு: Samsung Galaxy A17 5G W/O TA, 32,999 ரூபாய். இது உங்க பட்ஜெட்டை விட கொஞ்சம் அதிகம்."
        in said.text
    )
    assert "Fourth" not in said.text, "five names in a row is more than anyone holds"
    assert said.must_hear == frozenset({24_999.0, 20_999.0, 32_999.0})
    assert len(said.text) <= MOST_CHARS


def test_an_english_customer_hears_english():
    said = spoken.results(PICKS, "en")

    assert said is not None and said.text.startswith("One: Poco M8 5G, 24,999 rupees.")
    assert "This one is a little over your budget." in said.text


def test_a_card_says_price_offer_stock_and_emi_and_must_be_heard_with_all_four():
    product = {
        "name": "Samsung Galaxy A56 5G ( Awesome Graphite,8GB-256GB )",
        "price": 48_999,
        "price_with_best_offer": 46_999,
        "stock": {"state": "in", "branches": 46},
        "emi": {"per_month": 1_389},
    }

    said = spoken.card(product, "ta")

    assert said.text == (
        "Samsung Galaxy A56 5G, 48,999 ரூபாய். ஆஃபரோட 46,999 ரூபாய். "
        "46 கடைகள்ல ஸ்டாக் இருக்கு. EMI மாதம் 1,389 ரூபாய் முதல்."
    )
    assert said.must_hear == frozenset({48_999.0, 46_999.0, 46.0, 1_389.0})
    one_shop = spoken.card({**product, "stock": {"state": "in", "branches": 1}}, "en")
    assert "In stock at 1 shop." in one_shop.text


def test_a_lakh_is_said_the_way_a_price_tag_says_it():
    assert spoken.money(125_000, "ta") == "1,25,000 ரூபாய்"


# --- heard as said -------------------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "ok"),
    [
        # The ear writes the brand in Tamil script. Same words; price exact.
        ("சாம்சங் கேலக்ஸி A56, 48,999 ரூபாய்.", True),
        # These two were what the auditions came back as.
        ("Samsung Galaxy A56, 49,999 ரூபாய்.", False),
        ("Samsung Galaxy A56, 48,996 ரூபாய்.", False),
        ("Samsung Galaxy A56 ரூபாய்.", False),  # the price not said at all
        ("", False),
        ("Something else entirely, 48,999", False),
    ],
)
def test_a_sentence_is_played_only_if_its_price_was_heard_exactly(heard: str, ok: bool):
    meant = "Samsung Galaxy A56, 48,999 ரூபாய்."

    assert heard_as_said(meant, heard, frozenset({48_999.0})) is ok


# --- the book ------------------------------------------------------------------


class Mouth:
    def __init__(self, fail: bool = False) -> None:
        self.said: list[str] = []
        self.fail = fail

    async def say(self, text: str) -> tuple[bytes, float]:
        self.said.append(text)
        if self.fail:
            raise ModelUnavailable("the voice returned 429")
        return b"\x01\x00" * 4_800, 0.0002


class Ear:
    """Hears whatever it was built to hear, or the sentence itself."""

    def __init__(self, hears: str | None = None) -> None:
        self.hears = hears
        self.asked = 0
        self.last: str = ""

    async def transcribe(self, wav_bytes: bytes, lang: str = "ta") -> tuple[str, float]:
        self.asked += 1
        return (self.hears if self.hears is not None else self.last), 0.00003


def book(tmp_path, mouth: Mouth, ear: Ear, **kwargs) -> SpeechBook:
    made = SpeechBook(mouth, ear, tmp_path, **kwargs)
    original = mouth.say

    async def say_and_remember(text: str) -> tuple[bytes, float]:
        ear.last = text
        return await original(text)

    mouth.say = say_and_remember  # type: ignore[method-assign]
    return made


SAID = "Poco M8 5G, 24,999 ரூபாய்."
MUST = frozenset({24_999.0})


async def test_a_sentence_is_made_checked_kept_and_charged_once(tmp_path):
    mouth, ear, charged = Mouth(), Ear(), []
    speech = book(tmp_path, mouth, ear)

    key = speech.request(SAID, "ta", lambda most: True, charged.append, MUST)
    audio = await speech.fetch(key or "")
    again = speech.request(SAID, "ta", lambda most: False, charged.append, MUST)

    assert audio is not None and audio[:4] == b"RIFF"
    assert again == key, "a kept sentence needs no permission and no money"
    assert len(mouth.said) == 1 and ear.asked == 1
    assert len(charged) == 1 and charged[0] > 0


async def test_a_sentence_heard_with_the_wrong_price_is_never_played(tmp_path):
    mouth, ear, charged = Mouth(), Ear(hears="Poco M8 5G, 24,996 ரூபாய்."), []
    speech = book(tmp_path, mouth, ear)

    key = speech.request(SAID, "ta", lambda most: True, charged.append, MUST)

    assert await speech.fetch(key or "") is None
    assert list(tmp_path.glob("*.wav")) == []
    assert charged and charged[0] > 0, "wrong is still paid for, and said to be"
    assert (
        speech.request(SAID, "ta", lambda most: True, charged.append, MUST) is None
    ), "a sentence that failed is not paid for again per customer"
    assert len(mouth.said) == 1


async def test_without_the_governor_leave_nothing_is_made(tmp_path):
    mouth, ear = Mouth(), Ear()
    speech = book(tmp_path, mouth, ear)

    assert (
        speech.request(SAID, "ta", lambda most: False, lambda usd: None, MUST) is None
    )
    await asyncio.sleep(0)
    assert mouth.said == []


async def test_a_busy_voice_is_silence_not_an_error(tmp_path):
    speech = book(tmp_path, Mouth(fail=True), Ear())

    key = speech.request(SAID, "ta", lambda most: True, lambda usd: None, MUST)

    assert await speech.fetch(key or "") is None


async def test_two_customers_asking_at_once_pay_for_one_sentence(tmp_path):
    mouth, ear, charged = Mouth(), Ear(), []
    speech = book(tmp_path, mouth, ear)

    first = speech.request(SAID, "ta", lambda most: True, charged.append, MUST)
    second = speech.request(SAID, "ta", lambda most: True, charged.append, MUST)
    both = await asyncio.gather(speech.fetch(first or ""), speech.fetch(second or ""))

    assert first == second and all(both) and len(mouth.said) == 1 and len(charged) == 1


async def test_kept_sentences_survive_a_restart_and_the_oldest_go_first(tmp_path):
    mouth, ear = Mouth(), Ear()
    speech = book(tmp_path, mouth, ear, most_files=2)
    keys = []
    for price in (11_111, 22_222, 33_333):
        text = f"Phone, {price} ரூபாய்."
        key = speech.request(text, "ta", lambda most: True, lambda usd: None)
        await speech.fetch(key or "")
        keys.append(key)

    reopened = SpeechBook(Mouth(), Ear(), tmp_path)

    assert await reopened.fetch(keys[0] or "") is None, "the oldest was not dropped"
    assert await reopened.fetch(keys[2] or "") is not None


def test_too_long_or_empty_is_not_spoken(tmp_path):
    speech = SpeechBook(Mouth(), Ear(), tmp_path)

    assert speech.request("", "ta", lambda most: True, lambda usd: None) is None
    assert (
        speech.request(
            "க" * (MOST_CHARS + 1), "ta", lambda most: True, lambda usd: None
        )
        is None
    )


def test_the_same_words_in_another_voice_are_another_sentence(tmp_path):
    one = SpeechBook(Mouth(), Ear(), tmp_path, voice="ta-in-techagent-5")
    other = SpeechBook(Mouth(), Ear(), tmp_path, voice="ta-in-assistant-4")

    assert one.key(SAID) != other.key(SAID)


def test_a_line_recorded_in_another_voice_is_shown_not_played(tmp_path):
    """The owner, 2026-11-24: a second man's voice mid-call is improper.
    A take in any voice but the chosen one is never played - the line is
    shown on screen until it is recorded in the one voice."""
    from src.guided.voice import Recordings, wav, write_manifest

    lines = {"ask_brand": "எந்த பிராண்ட் வேணும்?", "say_tap": "தொட்டு சொல்லுங்க."}
    write_manifest(
        {
            "ask_brand": {"text": lines["ask_brand"], "voice": "ta-in-techagent-5"},
            "say_tap": {"text": lines["say_tap"], "voice": "live-standin"},
        },
        tmp_path,
    )
    for key in lines:
        (tmp_path / f"{key}.wav").write_bytes(wav(b"\x01\x00" * 2_400, 24_000))

    recordings = Recordings(tmp_path, lines, voice="ta-in-techagent-5")

    assert recordings.for_line(lines["ask_brand"]) == "ask_brand"
    assert recordings.for_line(lines["say_tap"]) is None
