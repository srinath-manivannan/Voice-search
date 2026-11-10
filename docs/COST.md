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

## A correction, and why it is at the top

An earlier version of this file reported **Rs 2.33 a connected minute** and
called the target met. The arithmetic was right and the configuration was
not: it assumed a 400-token context window, and probed against the live API a
window that small **ends the call on the first tool call** - close code 1007,
"Request contains an invalid argument" - because compression always keeps the
~1,430-token system instruction, so a 400-token trigger fires on every turn
and leaves the function call behind while its response is still coming.

Two real calls died that way before anyone looked, and the suite was green
throughout, because every cost test built its own shape instead of reading
the shipped config.

With a window that calls actually survive, a minute costs **Rs 3 to Rs 5**.
That is the number, and the rest of this file is written to it.

## Prediction

Shipped configuration: context window 3,000/2,400, fixed prompt ~1,430
tokens, agent speaking 63% of wall clock - the talk time measured on a real
call, and deliberately not reduced.

Cost per connected minute is **flat however long the call runs**, because the
window is capped. A ten-minute call costs ten times a one-minute call, not a
hundred times.

| generations/min | caller voice + audio history | prompt + tools + text history | agent voice | thinking | **total Rs/min** |
|---|---|---|---|---|---|
| 4.0 | 1.28 | 0.53 | 1.19 | 0.17 | **3.17** |
| 5.0 | 1.60 | 0.66 | 1.19 | 0.22 | **3.67** |
| 5.73 *(measured caller)* | 1.83 | 0.76 | 1.19 | 0.25 | **4.03** |
| 6.5 | 2.08 | 0.86 | 1.19 | 0.28 | **4.41** |
| 8.09 *(measured, incl. tool turns)* | 2.59 | 1.07 | 1.19 | 0.35 | **5.20** |

Generations, not caller turns: every tool call is a silent generation of its
own and re-bills the window like any other.

Poorvika's target is Rs 2.50. **We are over it**, by about Rs 1.50 a minute
at the measured caller's pace. The incumbent vendor charges Rs 4.85 a minute,
so the shipped configuration is close to it rather than comfortably under.
The target is shown on screen and coloured against; the session ceiling that
actually ends a call sits at Rs 8, because hanging up on a customer
mid-sentence does not refund the rupee.

### What the window is worth, and what it costs to go smaller

| window | Rs/min at 5.73 gen/min | behaviour, probed on the live API |
|---|---|---|
| prompt only (no history) | 2.28 | **impossible** - 400/100 closes the socket on the first tool call |
| 2,500/2,000 | ~3.3 | survives; agent answers "Endha model pathi kekureenga?" to everything |
| **3,000/2,400** | **4.03** | shipped - held eight turns and two compressions |
| 4,000/3,000 | 5.26 | also correct, Rs 1.2 dearer |
| uncapped | 8.99 | what the measured call did: Rs 8.33 billed |

So Rs 2.33 was never a configuration. It was the price of a window with no
conversation in it.

### What would actually close the gap

Not this knob. The window cannot go below the prompt, and the prompt is
charged on every generation whatever it is set to.

1. **Fewer generations.** Everything standing in the window is re-billed per
   generation, so the rate is close to linear in them. The tool-turn work
   took a measured conversation from 1.75 generations per caller turn to
   1.38, and stock and offers now cost no round trip at all.
2. **A smaller prompt.** 1,430 tokens, every generation, forever. It also
   sets the floor under the window, so cutting it twice.
3. **Not native audio for the whole call.** Text in and out on a cheaper
   model is roughly a tenth of these rates. That is an architecture decision,
   not a setting, and it is the only route to Rs 2.50 that the numbers
   support.

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

| change | worth |
|---|---|
| context window uncapped -> 3,000/2,400 | ~Rs 5/min on a three-minute call |
| fixed prompt 1,802 -> ~1,430 tokens | ~Rs 0.3/min, and it lowers the window's floor |
| tool round-trips removed | 1.75 -> 1.38 generations per caller turn, measured |
| agent's replies | **untouched** |

The first compression setting, 4,000/2,000, saved nothing on a three-minute
call: the window was larger than one ever accumulated, so it never fired. The
answer to that was not a smaller window than the prompt - that was the
mistake - it was accepting that compression only pays on long calls.

That any of this is safe depends on the conversation's state not living in
the window: the shortlist a caller refers back to is held server-side in
`ToolSession`, so "the second one" survives a compression that discards the
audio of being told about it. **The budget does not.** At turn eight of a
probed conversation the agent had lost the 20-25k the caller opened with,
because compression drops the oldest turn and that is where it was said. A
budget belongs beside the shortlist. Until it does, this is a known limit.

Every window value in this file was probed against the live endpoint before
being written down. This is the API that refuses `thinkingConfig` outright
and accepts a 400-token window that then kills the call, so neither
acceptance at setup nor one turn going well is evidence.

## Tool round-trips

The measured call had 17 caller turns but **24 generations**. Every tool call
is another turn — one that says nothing to make the call, another to answer —
and each re-bills the whole window. Five of the 24 said nothing at all and
burnt 413 thinking tokens doing it. At the shipped window those seven extra
generations are about Rs 1.2 a minute: Rs 4.03 at 5.73 generations a minute
against Rs 5.20 at 8.09.

What changed:

| | effect |
|---|---|
| stock + offers fetched **with the price**, one request | a stock question needs no round trip at all — structural |
| identical search answered from the previous result | saves the upstream call and ~2.3s, **not** the generation |
| re-search no longer rebuilds the shortlist | "the second one" keeps meaning the same phone |
| descriptions say the search result is complete and stays valid | fewer calls, if the model obeys |

Measured again after the change, on an eight-turn probe: **1.38 generations
per caller turn**, two tool calls in eight turns. Stock and offers were both
answered with no round trip at all, which was structurally impossible before.

By the time a duplicate search reaches us the model has already spent the
generation, so the memo cannot win that back. It buys latency and correctness.
The rest is a prompt matter, and it is measurable on the next trace rather
than assumable.
