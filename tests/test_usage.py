"""The cost meter has to get two Phase 0 findings right.

These usageMetadata blobs are copied verbatim from the measurement runs, not
invented, so a change in how the API reports usage shows up as a test failure
rather than as a quietly wrong invoice.
"""

import pytest

from src.voice.usage import FLASH_LITE, LIVE, SessionUsage, TurnUsage

# Turn 1 of the 5-turn run, 2026-11-03. Note thoughtsTokenCount sits outside
# totalTokenCount - 748 == 645 + 103, with the 90 thinking tokens nowhere in it.
TURN_1 = {
    "promptTokenCount": 645,
    "responseTokenCount": 103,
    "totalTokenCount": 748,
    "promptTokensDetails": [
        {"modality": "TEXT", "tokenCount": 335},
        {"modality": "AUDIO", "tokenCount": 283},
    ],
    "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 103}],
    "thoughtsTokenCount": 90,
}

TURN_2 = {
    "promptTokenCount": 877,
    "responseTokenCount": 194,
    "totalTokenCount": 1071,
    "promptTokensDetails": [
        {"modality": "TEXT", "tokenCount": 407},
        {"modality": "AUDIO", "tokenCount": 470},
    ],
    "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 194}],
    "thoughtsTokenCount": 46,
}


def test_thinking_tokens_are_billable_even_though_total_excludes_them():
    turn = TurnUsage.parse(TURN_1)
    assert (
        TURN_1["totalTokenCount"]
        == TURN_1["promptTokenCount"] + TURN_1["responseTokenCount"]
    )
    assert turn.thoughts == 90
    # The number that matters is output + thinking, which totalTokenCount omits.
    assert turn.billable_output == 103 + 90


def test_missing_thoughts_field_is_zero_not_a_crash():
    blob = {k: v for k, v in TURN_1.items() if k != "thoughtsTokenCount"}
    assert TurnUsage.parse(blob).thoughts == 0


def test_modalities_are_priced_separately():
    turn = TurnUsage.parse(TURN_1)
    assert turn.prompt_audio == 283
    assert turn.prompt_text == 335
    # Audio input is 4x the text rate, so collapsing them would misprice badly.
    expected = (283 * 3.00 + 335 * 0.75) / 1e6
    assert turn.prompt_usd() == expected


def test_the_prompt_is_charged_on_every_turn_not_once():
    """Google: "Past tokens are re-processed and accounted for in each new turn."

    So the charge is the SUM of the cumulative figures, not the last one. The
    difference on a real three-minute call was about 7x, which is why this is
    pinned by a test rather than left to a comment.
    """
    s = SessionUsage()
    s.record(TURN_1)
    s.record(TURN_2)

    one, two = TurnUsage.parse(TURN_1), TurnUsage.parse(TURN_2)
    assert s.input_usd == one.prompt_usd() + two.prompt_usd()
    # Which is strictly more than charging the final prompt once.
    assert s.input_usd > two.prompt_usd()
    # Output is charged once per turn - each reply is generated once.
    assert s.output_usd == one.output_usd() + two.output_usd()


def test_thoughts_token_count_is_on_the_bill_even_though_total_omits_it():
    """totalTokenCount is prompt + response. thoughtsTokenCount sits outside it.

    A meter that trusted the total would drop these tokens. They are billed,
    including on gemini-3.8-live, which cannot switch thinking off.
    """
    counted = SessionUsage()
    counted.record(TURN_1)
    blind = SessionUsage()
    blind.record(
        {key: value for key, value in TURN_1.items() if key != "thoughtsTokenCount"}
    )

    thinking = counted.breakdown(88.0, 0.18, 1.0).line("thinking")
    assert thinking.tokens == TURN_1["thoughtsTokenCount"]
    assert thinking.usd == pytest.approx(90 * 4.50 / 1e6)
    assert counted.total_usd == pytest.approx(blind.total_usd + thinking.usd)
    assert counted.billed_thinking == 90


def test_billed_prompt_tokens_are_summed_and_responses_are_not():
    s = SessionUsage()
    s.record(TURN_1)
    s.record(TURN_2)

    assert s.billed_in_audio == 283 + 470
    assert s.billed_in_text == 335 + 407
    assert s.billed_out_audio == 103 + 194
    assert s.billed_thinking == 90 + 46


def test_breakdown_is_per_connected_minute_including_gst():
    s = SessionUsage()
    s.record(TURN_1)
    s.record(TURN_2)
    b = s.breakdown(usd_inr=89.0, gst_rate=0.18, minutes=2.0)

    assert b.total_inr_per_min == pytest.approx(s.total_usd * 89.0 * 1.18 / 2.0)
    # Every line is present, every share sums to one, and the shares are the
    # point of the panel: the biggest one is the thing to go and fix.
    assert [line.key for line in b.lines] == [
        "in_audio",
        "in_text",
        "out_audio",
        "out_text",
        "thinking",
    ]
    assert sum(line.share for line in b.lines) == pytest.approx(1.0)


def test_the_voice_floor_is_less_than_the_bill():
    """Reported side by side so it is visible what can be engineered away."""
    s = SessionUsage()
    s.record(TURN_1)
    s.record(TURN_2)
    assert s.voice_usd < s.total_usd


def test_zero_minutes_is_rejected_rather_than_dividing_by_zero():
    s = SessionUsage()
    s.record(TURN_1)
    try:
        s.breakdown(89.0, 0.18, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_chat_is_priced_at_the_text_model_rate_not_the_audio_one():
    """Pricing chat at Live rates would overstate it about fivefold."""
    blob = {"promptTokenCount": 800, "candidatesTokenCount": 80}
    turn = TurnUsage.parse_text(blob)

    assert turn.prompt_text == 800 and turn.response_text == 80
    assert turn.prompt_usd(FLASH_LITE) == 800 * 0.30 / 1e6
    assert turn.output_usd(FLASH_LITE) == 80 * 2.50 / 1e6
    # The same turn on the Live model's text rates costs noticeably more.
    assert turn.output_usd(LIVE) > turn.output_usd(FLASH_LITE)


def test_a_session_prices_every_turn_with_its_own_model_rates():
    chat = SessionUsage(rates=FLASH_LITE)
    chat.record_text({"promptTokenCount": 400, "candidatesTokenCount": 60})
    voice = SessionUsage()
    voice.record_text({"promptTokenCount": 400, "candidatesTokenCount": 60})

    assert chat.output_usd < voice.output_usd
