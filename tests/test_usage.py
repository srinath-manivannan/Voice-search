"""The cost meter has to get two Phase 0 findings right.

These usageMetadata blobs are copied verbatim from the measurement runs, not
invented, so a change in how the API reports usage shows up as a test failure
rather than as a quietly wrong invoice.
"""

from src.voice.usage import SessionUsage, TurnUsage

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


def test_both_billing_hypotheses_are_reported():
    s = SessionUsage()
    s.record(TURN_1)
    s.record(TURN_2)

    # H1 charges every turn its full prompt; H2 charges the final prompt once.
    assert s.input_usd_rebilled > s.input_usd_billed_once
    assert s.input_usd_billed_once == TurnUsage.parse(TURN_2).prompt_usd()
    # Output is charged once per turn under either reading.
    assert (
        s.output_usd
        == TurnUsage.parse(TURN_1).output_usd() + TurnUsage.parse(TURN_2).output_usd()
    )


def test_report_is_per_connected_minute_including_gst():
    s = SessionUsage()
    s.record(TURN_1)
    r = s.report_inr(usd_inr=89.0, gst_rate=0.18, minutes=2.0)
    totals = s.totals_usd()
    assert r["billed_once_inr_per_min"] == totals["billed_once"] * 89.0 * 1.18 / 2.0
    assert r["rebilled_inr_per_min"] >= r["billed_once_inr_per_min"]


def test_zero_minutes_is_rejected_rather_than_dividing_by_zero():
    s = SessionUsage()
    s.record(TURN_1)
    try:
        s.report_inr(89.0, 0.18, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
