"""Pricing, and the prediction the configuration was sized from.

The numbers in here are the ones a commercial promise rests on, so the test
that matters most is the last one: the model, run against the shape of a real
measured call, has to land near what that call was actually billed. A cost
model nobody checked against an invoice is a spreadsheet.
"""

from __future__ import annotations

import pytest

from src.config import devconfig
from src.voice.cost import (
    FLASH_LITE,
    IN_AUDIO,
    IN_TEXT,
    LIVE,
    OUT_AUDIO,
    THINKING,
    TOKENS_PER_AUDIO_SECOND,
    Shape,
    growing_window,
    predict,
    price,
    steady_window,
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

# System instruction plus tool declarations, as the API counts them: the floor
# `prompt_text` fell back to on a live call once compression had thrown away
# everything else. Measured, not estimated, because every number below is a
# multiple of it - it is charged on every single turn and compression is
# documented to keep it.
FIXED_PROMPT = 1430


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
        fixed_text_tokens=FIXED_PROMPT,
        window_tokens=steady_window(
            devconfig.CONTEXT_TRIGGER_TOKENS, devconfig.CONTEXT_TARGET_TOKENS
        ),
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
        window_tokens=growing_window(1802, final_audio / 0.8),
    )
    b = predict(shape, usd_inr=USD_INR, gst_rate=GST, minutes=MEASURED["seconds"] / 60)
    modelled_ex_gst = b.total_usd * USD_INR
    # Within 15%, and on the high side - a cost model that flatters is worse
    # than one that does not.
    assert modelled_ex_gst == pytest.approx(MEASURED["billed_inr_ex_gst"], rel=0.15)
    assert modelled_ex_gst >= MEASURED["billed_inr_ex_gst"]


def test_the_shipped_configuration_is_nowhere_near_the_rs_2_50_target():
    """The number this project was asked for, and does not reach.

    It used to assert the opposite, and passed, because the shape it was
    given carried a 400-token window that no call can actually run on. With
    the window the calls actually survive on, a minute costs several rupees,
    and most of it is the context being re-processed on every generation.

    Asserting the truth here rather than the target is the point: a test that
    reports success against a configuration nobody can place a call on is
    worse than no test, because it is believed.
    """
    b = predict(
        _measured_shape(turns_per_minute=5.73),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert b.total_inr_per_min > 2.50
    # Pinned so the gap is a number someone can work against, not a feeling.
    assert 4.0 < b.total_inr_per_min < 7.0


def test_the_re_billed_history_is_the_bill_not_the_agents_voice():
    """Where the money actually goes, now that the window is a real one.

    This matters for what to do next. The agent's voice is charged once and
    the caller asked for it not to be cut; the retained audio history is
    charged again on every generation. The second is several times the first,
    so the levers that pay are the number of generations and the size of the
    window - not the length of the answers.
    """
    b = predict(
        _measured_shape(turns_per_minute=5.73),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert b.line(IN_AUDIO).inr_per_min > b.line(OUT_AUDIO).inr_per_min
    # The agent's voice is unchanged from the measured call: same talk time,
    # same tokens. Nothing was taken off the answers to get here.
    assert b.line(OUT_AUDIO).inr_per_min == pytest.approx(1.19, abs=0.02)


def test_halving_the_generations_nearly_halves_the_bill():
    """The one lever with real leverage, and the one the tool fix pulls.

    Everything standing in the window is charged again per generation, so the
    rate is close to linear in generations a minute. That is why a tool call
    - which costs a whole silent generation before the answering one - is
    worth removing, and why clipping replies is not.
    """
    busy = predict(
        _measured_shape(turns_per_minute=8.09),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    calm = predict(
        _measured_shape(turns_per_minute=4.05),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    saved = busy.total_inr_per_min - calm.total_inr_per_min
    assert saved > 2.0
    # Not exactly half: the agent's voice is charged once however many
    # generations it took, so it does not scale with them.
    assert calm.total_inr_per_min > busy.total_inr_per_min / 2


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


def test_compression_is_necessary_and_nowhere_near_sufficient():
    """Worth having, and not the thing that reaches the target.

    On a three-minute call an uncapped window costs roughly twice the shipped
    one, so compression stays. What it does not do is close the gap to
    Rs 2.50: the capped figure is still well over it, and no legal setting of
    this knob gets there, because the window cannot go below the prompt and
    the prompt is charged on every generation regardless.

    The earlier version of this test asserted the opposite of its own first
    line, and passed, against a 400-token window no call can run on.
    """
    capped = predict(_measured_shape(), usd_inr=USD_INR, gst_rate=GST, minutes=3.0)
    uncapped = predict(
        _measured_shape(window_tokens=growing_window(FIXED_PROMPT, 9000)),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert uncapped.total_inr_per_min > capped.total_inr_per_min * 1.5
    assert capped.total_inr_per_min > 2.50


def test_the_target_is_only_reachable_by_a_window_that_cannot_hold_a_call():
    """The reconciliation of an earlier claim, kept where it can be checked.

    Rs 2.33 a minute was reported here once and the arithmetic was right: a
    window holding nothing but the prompt does come in under Rs 2.50, because
    there is no history to re-process. What was wrong was treating that as a
    configuration. Probed against the live API, a window that small closes
    the socket on the first tool call; the smallest that keeps a conversation
    is about twice the prompt, and it costs roughly twice as much.

    So the target is not impossible in arithmetic and is out of reach in
    practice, and the two have to be stated together or the number misleads.
    """
    no_history = predict(
        _measured_shape(turns_per_minute=5.73, window_tokens=FIXED_PROMPT),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert no_history.line(IN_AUDIO).inr_per_min < 0.5
    assert no_history.total_inr_per_min < 2.50

    shipped = predict(
        _measured_shape(turns_per_minute=5.73),
        usd_inr=USD_INR,
        gst_rate=GST,
        minutes=3.0,
    )
    assert shipped.total_inr_per_min > 2 * no_history.total_inr_per_min * 0.8


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
