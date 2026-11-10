# What a call costs

Model cost only. Telephony, hosting and our own compute are not in here.
All rupee figures are inclusive of 18% GST at ₹89 to the dollar.

## The billing rule

One rule explains every number below, and it is Google's, in writing:

> "The API charges you per turn for all tokens present in the session context
> window. A 'turn' is defined as one user input and the model's corresponding
> response."
>
> "Past tokens are re-processed and accounted for in each new turn, up to your
> configured context window size. As a session lengthens, the cost per turn
> increases because the conversational history is re-processed."
>
> "The Live API is natively multimodal. It retains conversational history as
> raw audio tokens to preserve acoustic nuance and tone."
>
> "Audio billing: The API bills you for the accumulated native audio tokens at
> the standard audio input rate on every turn."

— [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices),
read 2026-11-10.

There is no context caching for the Live API: the pricing table has no caching
row and the `setup` message has no `cachedContent` field. Nothing discounts
the re-processing.

Three consequences follow, and every decision here is one of them.

1. **Memory is the most expensive thing the agent has.** A second of audio in
   the window is 25 tokens, re-billed at the audio input rate on every turn
   for as long as it stays.
2. **Turns are the multiplier.** The system instruction, the tool
   declarations and the whole retained history are charged again on each one.
3. **Clipping replies to save money is backwards.** A turn re-bills the whole
   window; speech is charged once. An avoided turn is worth several seconds of
   extra speech, so the complete answer is the cheap one.

## Rates

Per 1M tokens, gemini-3.8-live, [pricing](https://ai.google.dev/gemini-api/docs/pricing)
read 2026-11-10. Audio is tokenised at ~25 tokens per second.

| | text | audio |
|---|---|---|
| input | $0.75 | $3.00 |
| output (thinking included) | $4.50 | $12.00 |

Two things the docs leave open, both flagged on screen rather than buried:

- **Thinking.** The row is "Output price (including thinking tokens)" and
  carries both figures. Which applies to `thoughtsTokenCount` is not stated.
  Priced here at the text rate; if it is the audio rate, that line is 2.7x.
- **Audio input.** The table prints "$3.00 or $0.005/min", but 25 tok/s × 60 ×
  $3.00/M is $0.0045/min. Unexplained. We price the tokens the API reports.

## Prediction

Shipped configuration: context window 400/100, fixed prompt 1,131 tokens,
agent speaking 63% of wall clock — the talk time measured on a real call, and
deliberately not reduced.

Cost per connected minute is **flat however long the call runs**, because the
window is capped. That is the whole point of capping it: a ten-minute call
costs ten times a one-minute call, not a hundred times.

| turns/min | caller voice + audio history | prompt + tools + text history | agent voice | thinking | **total ₹/min** |
|---|---|---|---|---|---|
| 4.0 | 0.25 | 0.37 | 1.19 | 0.17 | **1.99** |
| 5.0 | 0.32 | 0.47 | 1.19 | 0.22 | **2.18** |
| 5.73 *(measured caller)* | 0.36 | 0.53 | 1.19 | 0.25 | **2.33** |
| 6.0 | 0.38 | 0.56 | 1.19 | 0.26 | **2.38** |
| 7.0 | 0.44 | 0.65 | 1.19 | 0.30 | **2.58** |
| 8.09 *(measured, incl. tool turns)* | 0.51 | 0.75 | 1.19 | 0.35 | **2.80** |

Turn rate is mostly the caller's choice, which is why the table is a range and
not a number. The enforced ceiling is ₹2.50 a connected minute on the total,
checked after every turn once a call is past one minute.

### The floor

₹1.39 a minute is physics and no setting touches it: the caller's microphone
at 42% of wall clock and the agent's mouth at 63%. One minute of continuous
agent speech is ₹1.89 at the published $0.018/min. Everything above the floor
is re-processing, and only that part can be engineered away.

## How it was validated

Run against the shape of `out/traces/call-1794290180.json` — 178s, 24 turns,
no compression — the model gives ₹22.65 ex GST where the server metered
₹20.96. 8% high, which is the side to be wrong on. `tests/test_cost.py` pins
it, because a cost model nobody checked against an invoice is a spreadsheet.

## What changed, and what each was worth

Measured call: **₹9.01/min → ₹2.33/min** at the caller's own turn rate.

| change | worth |
|---|---|
| context window 4,000/2,000 → 400/100 | ~₹4.5/min |
| fixed prompt 1,802 → 1,131 tokens | ~₹0.36/min |
| agent's replies | **untouched** |

The first compression setting, 4,000/2,000, **saved nothing**: the window was
larger than a three-minute call ever accumulates, so it never fired — ₹9.19/min
with it against ₹9.01 without. 400/100 is about thirteen seconds of audio.

That is only safe because the conversation's state does not live in the
window: the shortlist a caller refers back to is held server-side in
`ToolSession`, so "the second one" survives a compression that discards the
audio of being told about it.

400/100, 300/80 and 600/150 were all probed against the live endpoint before
shipping. This is the API that refuses `thinkingConfig` outright, so acceptance
cannot be assumed.

## Tool round-trips

The measured call had 17 caller turns but **24 generations**. Every tool call
is another turn — one that says nothing to make the call, another to answer —
and each re-bills the whole window. Five of the 24 said nothing at all and
burnt 413 thinking tokens doing it. Zero tool turns would be ₹2.37/min against
the ₹2.85 that call's shape costs now.

What changed:

| | effect |
|---|---|
| stock + offers fetched **with the price**, one request | a stock question needs no round trip at all — structural |
| identical search answered from the previous result | saves the upstream call and ~2.3s, **not** the generation |
| re-search no longer rebuilds the shortlist | "the second one" keeps meaning the same phone |
| descriptions say the search result is complete and stays valid | fewer calls, if the model obeys |

Honest accounting, at that call's shape:

| generations | ₹/min | |
|---|---|---|
| 24 | 2.85 | as it ran |
| 23 | 2.78 | minus the stock round-trip — structural, the data is there now |
| 21 | 2.64 | minus the two duplicate searches — needs the model to obey |
| 17 | 2.37 | no tool turns at all |

By the time a duplicate search reaches us the model has already spent the
generation, so the memo cannot win that back. It buys latency and correctness.
The rest is a prompt matter, and it is measurable on the next trace rather
than assumable.
