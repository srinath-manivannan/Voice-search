"""What a call costs, per component, in rupees a minute.

This module exists because the two numbers we used to report - "billed once"
and "re-billed" - were a guess each, and the guess was 7x wide. Google's Live
API best-practices page settles it, and it settles it against us:

    "The API charges you per turn for all tokens present in the session
     context window. A 'turn' is defined as one user input and the model's
     corresponding response."

    "Past tokens are re-processed and accounted for in each new turn, up to
     your configured context window size. As a session lengthens, the cost
     per turn increases because the conversational history is re-processed."

    "The Live API is natively multimodal. It retains conversational history
     as raw audio tokens to preserve acoustic nuance and tone."

    "Audio billing: The API bills you for the accumulated native audio tokens
     at the standard audio input rate on every turn."

                        - ai.google.dev/gemini-api/docs/live-api/best-practices
                          read 2026-11-10

Four consequences, and every cost decision in this project follows from them.

1. There is one billing model, not two. `input_usd_rebilled` was right and
   `input_usd_billed_once` was wishful. There is no context caching for the
   Live API - the pricing table has no caching row and `setup` has no
   `cachedContent` field - so nothing discounts the re-processing.

2. Memory is the most expensive thing the agent has. A second of audio kept
   in the context window is 25 tokens, re-billed at the audio input rate on
   every turn for as long as it stays. At eight turns a minute, keeping one
   minute of audio history costs more than the agent's own voice.

3. Turns are the multiplier. Everything standing in the context window -
   the system instruction, the tool declarations, the whole retained history
   - is charged again on each one. So a reply that fully answers is CHEAPER
   than a short one that gets asked again: see `TURN_COST_IN_SPEECH_SECONDS`.

4. The microphone gate earns its keep. gemini-3.8-live has proactive audio
   permanently enabled, and for that mode the docs say "input tokens are
   charged the entire time the Live API is listening". Audio we hold back is
   audio nobody is billed for, on this turn and on every turn after it.

Rates are per 1M tokens from aistudio.google.com/docs/pricing, read
2026-11-10. Two notes where the docs leave something open, because a silent
assumption here is money:

  - Thinking. The row is "Output price (including thinking tokens)" and it
    carries both $4.50 (text) and $12.00 (audio). Which one applies to
    `thoughtsTokenCount` is not stated anywhere. Thoughts are text, so this
    prices them at the text rate - and `THINKING_RATE_IS_UNCONFIRMED` says so
    on screen, because if Google bills them as audio that line is 2.7x what
    we show.
  - Audio input. The table prints "$3.00 or $0.005/min (audio)", and 25
    tokens/sec x 60 x $3.00/M is $0.0045/min, not $0.005. The discrepancy is
    unexplained. Token counts come from the API itself, so this prices the
    tokens we are actually told about and does not try to reconcile the two.
"""

from __future__ import annotations

from dataclasses import dataclass

# Live API audio tokenisation, stated twice in the docs: "native audio tokens
# accumulate rapidly (approximately 25 tokens per sec of audio)".
TOKENS_PER_AUDIO_SECOND = 25

# Whether thoughtsTokenCount bills at the text or the audio output rate is not
# documented. Shown beside the figure rather than hidden in a comment.
THINKING_RATE_IS_UNCONFIRMED = True


@dataclass(frozen=True)
class Rates:
    """USD per 1M tokens, for one model.

    Rates belong to a model, not to a module. Chat runs on Flash-Lite and
    voice on the Live model, and hard-coding one set meant the chat path would
    have been priced at audio rates - a 5x error in the direction that makes a
    project look affordable.
    """

    prompt_audio: float
    prompt_text: float
    response_audio: float
    response_text: float
    thinking: float


# gemini-3.8-live, verified 2026-11-10.
LIVE = Rates(
    prompt_audio=3.00,
    prompt_text=0.75,
    response_audio=12.00,
    response_text=4.50,
    thinking=4.50,
)

# gemini-3.5-flash-lite. Text only - audio rates are zero because it is never
# sent audio, and a non-zero figure there would hide a routing mistake.
FLASH_LITE = Rates(
    prompt_audio=0.0,
    prompt_text=0.30,
    response_audio=0.0,
    response_text=2.50,
    thinking=2.50,
)


# The five lines a call is actually charged on. Named, because "input" covers
# two things that differ by 4x in price and by everything in how you fix them.
IN_AUDIO = "in_audio"
IN_TEXT = "in_text"
OUT_AUDIO = "out_audio"
OUT_TEXT = "out_text"
THINKING = "thinking"

LABELS = {
    IN_AUDIO: "Caller voice + audio history",
    IN_TEXT: "Prompt + tools + text history",
    OUT_AUDIO: "Agent voice",
    OUT_TEXT: "Transcription",
    THINKING: "Thinking",
}


@dataclass(frozen=True)
class Line:
    """One charge line: how many tokens, what they cost, what share."""

    key: str
    tokens: int
    usd: float
    inr_per_min: float
    share: float

    @property
    def label(self) -> str:
        return LABELS.get(self.key, self.key)


@dataclass(frozen=True)
class Breakdown:
    """A whole call priced out, per component, per connected minute.

    `inr_per_min` figures are inclusive of GST, because that is the number
    Poorvika is held to. Everything is model cost: telephony, hosting and our
    own compute are elsewhere.
    """

    lines: tuple[Line, ...]
    minutes: float
    turns: int
    usd_inr: float
    gst_rate: float

    @property
    def total_inr_per_min(self) -> float:
        return sum(line.inr_per_min for line in self.lines)

    @property
    def total_usd(self) -> float:
        return sum(line.usd for line in self.lines)

    @property
    def total_inr(self) -> float:
        return self.total_usd * self.usd_inr * (1 + self.gst_rate)

    @property
    def turns_per_minute(self) -> float:
        return self.turns / self.minutes if self.minutes > 0 else 0.0

    def line(self, key: str) -> Line:
        for item in self.lines:
            if item.key == key:
                return item
        raise KeyError(key)

    def as_dict(self) -> dict[str, object]:
        """For the wire. The UI shows every line, so every line goes out."""
        return {
            "total_inr_per_min": round(self.total_inr_per_min, 3),
            "total_inr": round(self.total_inr, 2),
            "minutes": round(self.minutes, 3),
            "turns": self.turns,
            "turns_per_minute": round(self.turns_per_minute, 2),
            "gst_rate": self.gst_rate,
            "usd_inr": self.usd_inr,
            "thinking_rate_unconfirmed": THINKING_RATE_IS_UNCONFIRMED,
            "lines": [
                {
                    "key": line.key,
                    "label": line.label,
                    "tokens": line.tokens,
                    "inr_per_min": round(line.inr_per_min, 3),
                    "share": round(line.share, 3),
                }
                for line in self.lines
            ],
        }


def price(
    *,
    in_audio_tokens: int,
    in_text_tokens: int,
    out_audio_tokens: int,
    out_text_tokens: int,
    thinking_tokens: int,
    minutes: float,
    turns: int,
    rates: Rates = LIVE,
    usd_inr: float,
    gst_rate: float,
) -> Breakdown:
    """Price already-counted tokens. Token counts are BILLED counts.

    For the prompt lines that means summed across turns, not the last turn's
    cumulative figure - the docs say each turn is charged for everything then
    in the window, so the sum is the charge.
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    gross = usd_inr * (1 + gst_rate) / minutes
    raw = {
        IN_AUDIO: (in_audio_tokens, rates.prompt_audio),
        IN_TEXT: (in_text_tokens, rates.prompt_text),
        OUT_AUDIO: (out_audio_tokens, rates.response_audio),
        OUT_TEXT: (out_text_tokens, rates.response_text),
        THINKING: (thinking_tokens, rates.thinking),
    }
    priced = {key: tokens * rate / 1e6 for key, (tokens, rate) in raw.items()}
    total = sum(priced.values())

    lines = tuple(
        Line(
            key=key,
            tokens=raw[key][0],
            usd=priced[key],
            inr_per_min=priced[key] * gross,
            share=(priced[key] / total if total else 0.0),
        )
        # Fixed order, so a column does not move between renders.
        for key in (IN_AUDIO, IN_TEXT, OUT_AUDIO, OUT_TEXT, THINKING)
    )
    return Breakdown(
        lines=lines,
        minutes=minutes,
        turns=turns,
        usd_inr=usd_inr,
        gst_rate=gst_rate,
    )


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------
#
# A measured call tells you what one call cost. It does not tell you what the
# next one will, and under per-turn re-billing the honest answer is "it
# depends on the call's shape, and it rises with length unless the window is
# capped". So the shape is named, and the cost falls out of it.


@dataclass(frozen=True)
class Shape:
    """A call described in the handful of numbers its cost depends on.

    Every field here is something we measure from a trace or set in config,
    so a prediction can be checked against the call it predicted.
    """

    # How often the model is asked to generate. The multiplier on everything
    # standing in the context window, and the number with the most leverage.
    turns_per_minute: float
    # Fraction of wall clock the agent is speaking.
    agent_talk_fraction: float
    # Fraction of wall clock the microphone gate actually forwards.
    caller_audio_fraction: float
    # Thinking tokens per turn.
    thinking_per_turn: float
    # System instruction plus tool declarations. Sliding-window compression
    # explicitly keeps these: "System instructions and any
    # BidiGenerateContentSetup.prefixTurns will always remain at the beginning
    # of the result." So they are paid for on every single turn, forever.
    fixed_text_tokens: int
    # contextWindowCompression. History sawtooths between target and trigger,
    # so the average standing in the window is the midpoint.
    trigger_tokens: int
    target_tokens: int
    # How much of retained history is audio rather than text. Audio costs 4x,
    # and on a voice call history is mostly audio.
    history_audio_fraction: float = 0.8
    # Text tokens the model emits per turn for output transcription. Charged
    # at the text OUTPUT rate, and only because we asked for transcripts.
    transcript_text_per_turn: float = 0.0


def predict(
    shape: Shape,
    *,
    rates: Rates = LIVE,
    usd_inr: float,
    gst_rate: float,
    minutes: float = 1.0,
) -> Breakdown:
    """Steady-state cost of a call of this shape.

    "Steady state" is the point of capping the window: without compression
    cost per minute climbs for as long as the call lasts, and with it the
    history stops growing and the rate goes flat. So this predicts one number
    for a call of any length, which is only true because the window is capped.
    """
    history = (shape.trigger_tokens + shape.target_tokens) / 2
    history_audio = history * shape.history_audio_fraction
    history_text = history - history_audio

    turns = shape.turns_per_minute * minutes
    seconds = minutes * 60

    # Per turn, the window holds the fixed prompt plus retained history. The
    # caller's new audio for the turn is already inside that history figure -
    # counting it again here would bill the same seconds twice.
    in_audio = turns * history_audio
    in_text = turns * (shape.fixed_text_tokens + history_text)

    # A floor under in_audio: a short call has not yet accumulated enough
    # history to reach the sawtooth, but it has certainly been charged for the
    # audio the caller sent.
    heard = shape.caller_audio_fraction * seconds * TOKENS_PER_AUDIO_SECOND
    in_audio = max(in_audio, heard)

    return price(
        in_audio_tokens=round(in_audio),
        in_text_tokens=round(in_text),
        out_audio_tokens=round(
            shape.agent_talk_fraction * seconds * TOKENS_PER_AUDIO_SECOND
        ),
        out_text_tokens=round(turns * shape.transcript_text_per_turn),
        thinking_tokens=round(turns * shape.thinking_per_turn),
        minutes=minutes,
        turns=round(turns),
        rates=rates,
        usd_inr=usd_inr,
        gst_rate=gst_rate,
    )


def turn_cost_in_speech_seconds(shape: Shape, rates: Rates = LIVE) -> float:
    """How many seconds of extra speech one avoided turn pays for.

    This is the number that decides whether to make the agent terser. Each
    turn re-bills the whole window; each extra second of speech is 25 tokens
    of audio output, charged once. If this comes out well above zero - it
    does, by a wide margin - then answering fully is the CHEAP option and
    clipping replies to save money is backwards: the clipped answer gets
    asked again, and the follow-up turn costs more than the sentence saved.
    """
    history = (shape.trigger_tokens + shape.target_tokens) / 2
    audio = history * shape.history_audio_fraction
    text = history - audio + shape.fixed_text_tokens
    per_turn = (
        audio * rates.prompt_audio
        + text * rates.prompt_text
        + shape.thinking_per_turn * rates.thinking
    ) / 1e6
    per_second = TOKENS_PER_AUDIO_SECOND * rates.response_audio / 1e6
    return per_turn / per_second if per_second else 0.0
