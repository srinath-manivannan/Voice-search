"""Tamil number words, written back as digits.

The first class is the transcripts the cascade's ear actually produced. The
rest are the two bugs that cost a round of debugging each, and the things
this deliberately refuses to read.
"""

from __future__ import annotations

import pytest

from src.agent.prefetch import Intent, read_intent
from src.agent.tamil_numbers import digitise, value_of


class TestWhatTheEarActuallySaid:
    def test_the_measured_transcript(self):
        """Spoken as "20000 kulla ... 25000 varaikkum", heard as words."""
        heard = (
            "எனக்கு இருபதாயிரம் குள்ள ஒரு நல்ல கேமரா போன் வேணும், இருபத்தி ஐந்தாயிரம் வரைக்கும் ஓகே."
        )
        assert digitise(heard) == (
            "எனக்கு 20000 குள்ள ஒரு நல்ல கேமரா போன் வேணும், 25000 வரைக்கும் ஓகே."
        )

    def test_the_prefetch_can_now_read_it(self):
        """The whole point: a budget a regular expression can find."""
        heard = "எனக்கு இருபதாயிரம் குள்ள ஒரு நல்ல கேமரா போன் வேணும்"
        assert read_intent(heard) == Intent(
            category="phone", min_price=None, max_price=20000
        )

    def test_up_to_is_a_ceiling_too(self):
        """வரைக்கும் came off the cascade's ear and had never been seen.

        The Live API romanises, so the Tamil-script ceiling words only
        appeared once there was a second transcriber.
        """
        assert read_intent("இருபத்தி ஐந்தாயிரம் வரைக்கும் ஒரு மொபைல்") == Intent(
            category="phone", min_price=None, max_price=25000
        )


class TestTheSandhi:
    """ஐந்து + ஆயிரம் is written ஐந்தாயிரம், and the ஆ becomes a vowel sign.

    The first version stripped "ஆயிரம்" off the end, which is a letter the
    fused word does not contain at all, so it matched none of the four forms
    the ear produced.
    """

    @pytest.mark.parametrize(
        ("said", "number"),
        [
            ("ஆயிரம்", 1000),
            ("ஐந்தாயிரம்", 5000),
            ("பத்தாயிரம்", 10000),
            ("இருபதாயிரம்", 20000),
            ("முப்பதாயிரம்", 30000),
            ("நாற்பதாயிரம்", 40000),
            ("ஐம்பதாயிரம்", 50000),
            ("பதினைந்தாயிரம்", 15000),
        ],
    )
    def test_a_fused_thousand(self, said: str, number: int):
        assert digitise(said) == str(number)

    @pytest.mark.parametrize(
        ("said", "number"),
        [
            ("இருபத்தி ஐந்தாயிரம்", 25000),
            ("முப்பத்தி ஐந்தாயிரம்", 35000),
            ("நாற்பத்தி ஐந்தாயிரம்", 45000),
        ],
    )
    def test_a_tens_prefix_scales_with_what_follows(self, said: str, number: int):
        """இருபத்தி ஐந்தாயிரம் is (20 + 5) x 1000, not 20 and then 5,000."""
        assert digitise(said) == str(number)

    def test_the_spaced_form_too(self):
        assert digitise("இருபத்தி ஐந்து ஆயிரம்") == "25000"


class TestTheLakhBug:
    def test_one_lakh_is_one_lakh(self):
        """A bare லட்சம் was listed as a fused form, so it counted twice.

        Stripping it left an empty stem, an empty stem means one, and the ஒரு
        before it had already been counted - "ஒரு லட்சம்" came out as two
        hundred thousand.
        """
        assert digitise("ஒரு லட்சம்") == "100000"

    def test_a_bare_lakh_is_still_a_lakh(self):
        assert digitise("லட்சம்") == "100000"


class TestWhatItLeavesAlone:
    def test_a_sentence_with_no_numbers(self):
        assert digitise("நல்ல கேமரா போன் வேணும்") == "நல்ல கேமரா போன் வேணும்"

    def test_the_article_is_not_the_number_one(self):
        """ "ஒரு போன்" is "a phone". Writing 1 there puts a digit by a noun."""
        assert digitise("ஒரு போன் வேணும்") == "ஒரு போன் வேணும்"

    def test_a_comma_ends_a_number(self):
        """Two budgets being compared, not one of fifty thousand."""
        assert digitise("இருபதாயிரம், முப்பதாயிரம்") == "20000, 30000"

    def test_halves_are_not_read_at_all(self):
        """ஒன்றரை லட்சம் is 150,000 and this returns it unread.

        Reading it as 100,000 would be a budget wrong by fifty thousand,
        which is worse than a budget the prefetch simply does not fire on -
        the model's own reading of the sentence is still behind that one.
        """
        assert "150000" not in digitise("ஒன்றரை லட்சம் வரை")
        assert "100000" not in digitise("ஒன்றரை லட்சம் வரை")

    def test_latin_digits_are_untouched(self):
        assert digitise("20000 kulla phone") == "20000 kulla phone"


class TestTheAdding:
    def test_a_run_that_is_not_a_number(self):
        assert value_of(["நல்ல", "போன்"]) is None

    def test_an_empty_run(self):
        assert value_of([]) is None

    def test_a_scale_with_nothing_in_front_is_one_of_it(self):
        assert value_of(["ஆயிரம்"]) == 1000
