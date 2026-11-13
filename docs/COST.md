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
- **Transcripts.** `inputAudioTranscription` and `outputAudioTranscription`
  are both on. Google charges the text they emit at the **text output**
  rate, $4.50 per million, on top of the audio. The Live Transcribe model
  is estimated at about 175 text tokens per minute of speech. The measured
  caller speaks 42% of a connected minute, so the caller line is about
  75 tokens, or **₹0.03 to ₹0.04** at ₹89 and 18% GST (about ₹0.03 at ₹88).
  A minute of continuous caller speech would be about ₹0.08. The meter does
  not estimate this: `response_text` on the usage block is priced as
  "Transcripts, caller and agent". Output transcription was already in that
  line. Input transcription is the new part of it.

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
| 6.07 *(no tool calls at all)* | 2.07 | 0.86 | 1.19 | 0.28 | **4.20** |
| 8.24 *(measured, after the tool fix)* | 2.64 | 1.09 | 1.19 | 0.36 | **5.28** |
| 8.42 *(the call as it ran)* | 2.69 | 1.11 | 1.19 | 0.37 | **5.36** |
| 10.36 *(a tool-heavy stretch)* | 3.18 | 1.31 | 1.19 | 0.45 | **6.33** |

**Generations, not caller turns**, and the distinction is the whole table.
The measured caller spoke 5.73 times a minute; with the greeting and the tool
calls that is 8.24 generations, and every one of them re-bills the window.
An earlier version of this file read 5.73 off the trace and wrote it in this
column, which understated the rate by about a rupee.

Poorvika's target is Rs 2.50. **We are over it, and over the incumbent's
Rs 4.85 as well.** The target is Rs 2.50, shown on screen and coloured
against; the session ceiling that actually ends a call sits at Rs 8, because hanging up on
a customer mid-sentence does not refund the rupee.

### One lever at a time, from Rs 5.28

| lever | Rs/min | change |
|---|---|---|
| prompt 1,430 -> 900 tokens | 4.93 | -0.34 |
| prompt 1,430 -> 700 tokens | 4.80 | -0.47 |
| window 3,000/2,400 -> 2,600/2,100 | 4.51 | -0.77 |
| generations 1.38 -> 1.10 per caller turn | 4.48 | -0.80 |
| agent talks 53% of the call, not 63% | 5.09 | -0.19 |
| thinking halved | 5.10 | -0.18 |
| caller speaks half as often | 3.64 | -1.64 |

The last one is the caller's, not ours. The two before it are refused: the
agent's replies are not being clipped, and `thinkingConfig` is rejected by
this model outright. Everything we control, pulled together and pulled hard,
lands near **Rs 3.40** - and carries risk, because 2,600/2,100 is one step
above a window measured to lose the conversation.

### Why compression cannot be the answer on a three-minute call

Google's Cloud documentation gives `trigger_tokens` a **minimum of 5,000**.
A three-minute call accumulates roughly 6,500 tokens in total, so a trigger
at the documented floor fires about once before the caller hangs up.
Compression is built to keep long sessions alive, not to make short calls
cheap. Every setting that does make a short call meaningfully cheaper is
below Google's stated minimum - including the 3,000 shipped here, which is
measured working and is not documented to work.

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

**Decided, and written up in [PIPELINE.md](PIPELINE.md):** not the cascade.
Its reasoning leg was timed at ~1,100 ms to first token, which makes the
whole thing about three times slower than today for 60 paise. Rotating the
Live session onto a fresh socket carrying a text summary instead of audio
history lands at Rs 2.70-3.82 at unchanged speed, and fixes the budget
amnesia as a side effect. The rest of this section is the arithmetic.

Not this knob, and not the answers. Within the Live API, the window cannot
go below the prompt, the prompt is charged on every generation, and the
agent's own voice is charged once at a rate nothing discounts.

**1. Fewer generations.** Everything in the window is re-billed per
generation, so the rate is close to linear in them. The tool-turn work took a
measured conversation from 1.75 generations per caller turn to 1.38, and
stock and offers now cost no round trip at all. Another 0.28 off that ratio
is worth Rs 0.80 a minute.

**2. A smaller prompt.** 1,430 tokens, every generation, forever - and it
sets the floor under the window, so it cuts twice. Worth about Rs 0.47 if it
comes down to 700.

Those two together, with a tighter window, reach roughly **Rs 3.40**. They do
not reach Rs 2.50, and nothing inside this architecture does.

**3. Stop using native audio for the whole call.** Transcribe the caller,
reason in text, speak with a TTS engine. History then lives as text at
$0.30/M instead of audio at $3.00/M, which is where four fifths of the bill
currently goes.

| component | per connected minute |
|---|---|
| STT - flash-lite audio in, transcript out | Rs 0.11 |
| text turn - prompt and history re-billed | Rs 0.76 |
| text turn - reply and thinking | Rs 0.39 |
| TTS - Chirp 3: HD, $30/M characters | Rs 1.43 |
| TTS - Neural2, $16/M characters | Rs 0.76 |
| TTS - Standard, $4/M characters | Rs 0.19 |

| build | Rs/min |
|---|---|
| cascade with Chirp 3: HD | **2.69** |
| cascade with Neural2 | **2.03** |
| cascade with Standard voices | **1.46** |
| Live native audio, today | 5.28 |

Rates read 2026-11-10 from Google's Text-to-Speech pricing and the Gemini
API pricing page. Two things this costing does not price, and both are real:

- **Tanglish does not survive a TTS engine.** The agent writes Tamil in
  English letters, and a synthesiser handed "Edhu paakkalaam?" will read it
  as English. Going cascade means committing to Tamil script for the voice
  surface and keeping the romanised form for chat.
- **Latency roughly doubles.** Native audio answers in about 500 ms today.
  Transcribe, reason, then synthesise is nearer 900-1,400 ms - inside the
  Phase 1 target of 1 s p50 and 2 s p95, with far less room.

Barge-in and endpointing would have to be ours as well, which they nearly
already are: the VAD and the microphone gate exist.

### The floor

Rs 1.39 a minute is physics and no setting touches it: the caller's microphone
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
