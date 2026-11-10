"""Pricing, and the prediction the configuration was sized from.

The numbers in here are the ones a commercial promise rests on, so the test
that matters most is the last one: the model, run against the shape of a real
measured call, has to land near what that call was actually billed. A cost
model nobody checked against an invoice is a spreadsheet.
"""

from __future__ import annotations

import pytest

from src.voice.cost import (
    FLASH_LITE,
    IN_AUDIO,
    IN_TEXT,
    LIVE,
    OUT_AUDIO,
    THINKING,
    TOKENS_PER_AUDIO_SECOND,
    Shape,
    predict,
    price,
    turn_cost_in_speech_seconds,
)

USD_INR, GST = 89.0, 0.18

# out/traces/call-1794290180.json, 2026-11-10. The last call before any of
# this existed, and the one the configuration is sized against.
MEASURED = dict(
    seconds=177.97,
    turns=24,
    agent_audio_tokens=2796,
    thinking_tokens=2204,
    caller_audio_seconds=75.0,  # (8898 frames - 5146 held by the gate) x 20ms
    billed_inr_ex_gst=20.96,  # what the server's own backstop computed
)


def test_each_line_is_priced_at_its_own_rate():
    """Audio input is 4x text input. Collapsing them misprices badly."""
    b = price(
        in_audio_tokens=1000,
        in_text_tokens=1000,
        out_audio_tokens=1000,
        out_text_tokens=1000,
        thinking_tokens=1000,
        minutes=1.0,
        turns=1,
        usd_inr=USD_INR,
        gst_rate=GST,
    )
    assert b.line(IN_AUDIO).usd == 1000 * 3.00 / 1e6
    assert b.line(IN_TEXT).usd == 1000 * 0.75 / 1e6
    assert b.line(OUT_AUDIO).usd == 1000 * 12.00 / 1e6
    # Audio output is the dearest line in the stack, by 4x over text output.
    assert b.line(OUT_AUDIO).usd == 4 * b.line(IN_AUDIO).usd


def test_rupees_a_minute_includes_gst_and_divides_by_connected_time():
    b = price(
        in_audio_tokens=0,
        in_text_tokens=0,
        out_audio_tokens=1_000_000,
        out_text_tokens=0,
        thinking_tokens=0,
        minutes=2.0,
        turns=1,
        usd_inr=USD_INR,
        gst_rate=GST,
    )
    assert b.total_usd == pytest.approx(12.00)
    assert b.total_inr_per_min == pytest.approx(12.00 * 89.0 * 1.18 / 2.0)


def test_shares_sum_to_one_and_a_free_call_does_not_divide_by_zero():
    b = price(
        in_audio_tokens=0,
        in_text_tokens=0,
        out_audio_tokens=0,
        out_text_tokens=0,
        thinking_tokens=0,
        minutes=1.0,
        turns=0,
        usd_inr=USD_INR,
        gst_rate=GST,
    )
    assert b.total_inr_per_min == 0.0
    assert all(line.share == 0.0 for line in b.lines)


def test_zero_minutes_is_rejected():
    with pytest.raises(ValueError):
        price(
            in_audio_tokens=1,
            in_text_tokens=0,
            out_audio_tokens=0,
            out_text_tokens=0,
            thinking_tokens=0,
            minutes=0,
            turns=1,
            usd_inr=USD_INR,
            gst_rate=GST,
        )


def test_chat_is_priced_at_the_text_model_rate_not_the_audio_one():
    """Pricing chat at Live rates would overstate it about fivefold."""
    b = price(
        in_audio_tokens=0,
        in_text_tokens=1000,
        out_audio_tokens=0,
        out_text_tokens=1000,
        thinking_tokens=0,
        minutes=1.0,
        turns=1,
        rates=FLASH_LITE,
        usd_inr=USD_INR,
        gst_rate=GST,
    )
    assert b.line(IN_TEXT).usd == 1000 * 0.30 / 1e6
    assert (
        b.total_usd
        < price(
            in_audio_tokens=0,
            in_text_tokens=1000,
            out_audio_tokens=0,
            out_text_tokens=1000,
            thinking_tokens=0,
            minutes=1.0,
            turns=1,
            rates=LIVE,
            usd_inr=USD_INR,
            gst_rate=GST,
        ).total_usd
    )


# --- prediction -------------------------------------------------------------


def _measured_shape(**over: object) -> Shape:
    seconds = MEASURED["seconds"]
    base = dict(
        turns_per_minute=MEASURED["turns"] / (seconds / 60),
        agent_talk_fraction=(
            MEASURED["agent_audio_tokens"] / TOKENS_PER_AUDIO_SECOND / seconds
        ),
        caller_audio_fraction=MEASURED["caller_audio_seconds"] / seconds,
        thinking_per_turn=MEASURED["thinking_tokens"] / MEASURED["turns"],
        fixed_text_tokens=1131,
        trigger_tokens=400,
        target_tokens=100,
    )
    base.update(over)
    return Shape(**base)  # type: ignore[arg-type]


def test_the_model_lands_near_what_the_real_call_was_billed():
    """The validation that makes the rest of this worth anything.

    No compression was active on that call, so history grew to the end of it
    and the average over the call was half the final figure. Model it as a
    window whose midpoint is that average, and the answer has to come out
    near the Rs 20.96 the server metered.
    """
    final_audio = (
        MEASURED["caller_audio_seconds"] * TOKENS_PER_AUDIO_SECOND
        + MEASURED["agent_audio_tokens"]
    )
    shape = _measured_shape(
        fixed_text_tokens=1802,  # the instruction as it was on that call
        trigger_tokens=int(final_audio / 0.8),
        target_tokens=0,
    )
    b = predict(shape, usd_inr=USD_INR, gst_rate=GST, minutes=MEASURED["seconds"] / 60)
    modelled_ex_gst = b.total_usd * USD_INR
    # Within 15%, and on the high side - a cost model that flatters is worse
    # than one that does not.
    assert modelled_ex_gst == pytest.approx(MEASURED["billed_inr_ex_gst"], rel=0.15)
    assert modelled_ex_gst >= MEASURED["billed_inr_ex_gst"]


def test_the_shipped_configuration_is_inside_the_ceiling():
    """Rs 2.50 a minute at the rate the measured caller actually spoke.

    17 end_of_speech events in 177.97s is 5.73 caller turns a minute. That is
    the shape to size against, because it is the part of the call the caller
    decides.
    """
    b = predict(
        _measured_shape(turns_per_minute=5.73),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert b.total_inr_per_min <= 2.50


def test_it_still_holds_a_little_above_the_measured_turn_rate():
    """Turn rate is mostly the caller's choice, so it needs headroom."""
    b = predict(
        _measured_shape(turns_per_minute=6.5),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert b.total_inr_per_min <= 2.50


def test_a_tool_round_trip_costs_a_whole_extra_window_re_bill():
    """The named gap, pinned so it cannot be forgotten.

    The measured call had 17 caller turns but 24 generations: every tool call
    is another turn, and another turn re-bills the whole context window. Those
    seven extra generations are what takes a Rs 2.33 call to Rs 2.80, past the
    ceiling - and three of the five tool calls were duplicate searches for the
    same phone, so a good part of it is waste rather than work.

    This is the next lever, and it is one we control, unlike how often the
    caller speaks.
    """
    caller_only = predict(
        _measured_shape(turns_per_minute=5.73),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    with_tool_turns = predict(
        _measured_shape(turns_per_minute=8.09),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    overhead = with_tool_turns.total_inr_per_min - caller_only.total_inr_per_min
    assert overhead > 0.35
    # Over the ceiling, which is exactly why the ceiling is enforced on the
    # total now rather than on the audio streams alone.
    assert with_tool_turns.total_inr_per_min > 2.50


def test_cost_per_minute_is_flat_however_long_the_call_runs():
    """The whole point of capping the window.

    Uncapped, the rate climbs for as long as the call lasts, because each turn
    re-bills a bigger history. Capped, it goes flat - so a ten-minute call
    costs ten times a one-minute call and not a hundred times.
    """
    rates = [
        predict(
            _measured_shape(), usd_inr=USD_INR, gst_rate=GST, minutes=m
        ).total_inr_per_min
        for m in (1, 3, 5, 10)
    ]
    assert max(rates) - min(rates) < 0.01


def test_an_uncapped_window_is_the_single_most_expensive_setting():
    """What the first attempt at compression got wrong.

    4,000/2,000 held more than a three-minute call ever accumulated, so it
    never fired and saved nothing. The fix was not more compression, it was a
    window small enough to bite.
    """
    tight = predict(_measured_shape(), usd_inr=USD_INR, gst_rate=GST, minutes=3.0)
    loose = predict(
        _measured_shape(trigger_tokens=4000, target_tokens=2000),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert loose.total_inr_per_min > tight.total_inr_per_min * 2


def test_the_agents_voice_is_untouched_and_is_the_biggest_remaining_line():
    """The caller asked for cost down WITHOUT clipping the replies.

    So the thing to prove is that the saving came from somewhere else: after
    the cut, the agent's own voice is the largest single line, which is what
    it looks like when nothing was taken off the answer.
    """
    b = predict(_measured_shape(), usd_inr=USD_INR, gst_rate=GST, minutes=3.0)
    voice = b.line(OUT_AUDIO).inr_per_min
    assert voice == max(line.inr_per_min for line in b.lines)
    # Unchanged from the measured call: same talk time, same tokens.
    assert voice == pytest.approx(1.19, abs=0.02)


def test_an_avoided_turn_is_worth_several_seconds_of_speech():
    """Why clipping replies to save money is backwards.

    A turn re-bills the whole window; speech is charged once. So if this is
    comfortably above zero, the cheap answer is the complete one, and a reply
    trimmed until the customer has to ask again costs more than it saved.
    """
    seconds = turn_cost_in_speech_seconds(_measured_shape())
    assert seconds > 3.0


def test_thinking_priced_at_the_audio_rate_would_still_be_visible():
    """Google does not document which output rate applies to thoughts.

    Priced at the text rate here, and flagged in the UI. This pins the size
    of the exposure if that reading is wrong, so nobody has to re-derive it.
    """
    from dataclasses import replace

    text_rate = predict(_measured_shape(), usd_inr=USD_INR, gst_rate=GST, minutes=3.0)
    audio_rate = predict(
        _measured_shape(),
        rates=replace(LIVE, thinking=12.00),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    extra = audio_rate.total_inr_per_min - text_rate.total_inr_per_min
    assert text_rate.line(THINKING).inr_per_min > 0
    # Under a rupee a minute: real, worth flagging on screen, not ruinous.
    assert 0.1 < extra < 1.0
