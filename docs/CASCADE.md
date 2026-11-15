# Rs 2.50, and what it costs to get there

Written 2026-11-15, after the customer set Rs 2.50 as a ceiling rather than a
target: thinking, history, transcription, voice, everything in.

[PIPELINE.md](PIPELINE.md) chose rotation over the cascade on 2026-11-12 and
said why in one line: *"Rs 2.50 with a fast agent is not on the table; Rs 2.70
is, and it is half of today."* That trade has now been decided the other way.
This file is the reversal, the arithmetic behind it, and the thing it costs.

**Nothing here is built yet.** Every figure is either measured on this project
or read off a published rate, and each one says which.

## The decision in one table

| build | Rs/min | first audio | Tanglish | state |
|---|---|---|---|---|
| Live native audio | **3.07** | ~500 ms | yes | shipped today |
| **Whisper + flash-lite + Live mouth** | **1.66 - 2.27** | **~1,800 ms** | **yes** | **this plan** |
| Whisper + flash-lite + Chirp 3: HD | 1.90 - 2.51 | ~1,550 ms | no | Tamil script only |
| Whisper + flash-lite + Standard voice | 0.66 - 1.27 | ~1,500 ms | no | robotic |

The range is history: how much text stands in the window, which in a cascade
is ours to decide rather than the API's. The table in
[the cost section](#the-cost-leg-by-leg) shows it.

**The recommendation is row two.** It is under the ceiling with room to spare,
it keeps Tanglish, and the mouth is the one piece of today's build that
already works and already rotates.

**What it costs is 1.3 seconds on every single turn the customer waits
through.** That is the whole trade and it should not be read past.

---

## 1. What the incumbent actually does

Driven on reliancedigital.in on 2026-11-15, five exchanges. The customer's
screenshots show the opening interview; what follows is what the rest of the
conversation does.

### The shape of a reply

Asked *"I need a phone between 20000 and 25000"*, EXA did **not** interview.
It answered, with five products, and closed with one question:

> Want me to narrow it down by **brand, camera, or battery/performance**?

Answered *"camera"*, it came back with **one strongest pick, a reason from the
specs, two alternatives, and one more question**:

> Got it — for **camera** in Rs 20k-25k, this one's the strongest pick
> - **Redmi 15C 5G (8GB/128GB)** - 50 MP rear + 8 MP front - **Rs 22,499** (34% OFF)
>
> Other options in this range: [two more]
>
> Want me to optimize for **selfies** or **rear camera**?

That is the pattern, and it is a good one: **answer first, then narrow by
one binary question.** The interview only happens when the customer clicks
the "Help me find the right phone" chip - that is, when they have said
nothing at all to go on.

### Three things it gets right

1. **Never makes you answer before it gives you something.** A customer who
   said a budget gets products for that budget on the next breath.
2. **One question at a time, and always a choice of two.** "Selfies or rear
   camera" is answerable in one word. "What are you looking for" is not.
3. **It carries context.** Six turns in, Rs 20k-25k was still in force
   without being repeated.

### Three things it gets wrong

1. **It broke the budget.** Asked in Tanglish for a camera phone *under*
   Rs 20,000, it offered **Rs 20,999 and Rs 21,999** - and then said:
   *"Rs 20,000 kulla strict-aave vendum-naa sollunga"* - tell me if you
   really meant under 20,000 and I'll filter. The customer stated a
   constraint and the agent asked to be let off it. On a phone call, where
   nothing is on screen to check, this is the failure that loses trust.
2. **No live store stock.** Asked whether a phone was in stock at the T Nagar
   Chennai store, it said it could not confirm store stock in chat and sent
   the customer to the product page to check for themselves.
3. **Five products at once.** Readable on a screen. Unspeakable on a call.

### What it did with Tamil

Asked in Tanglish - *"enakku 20000 kulla oru nalla camera phone venum"* - it
replied in **Tamil script with English product terms**:

> sari - Rs 20,000 kulla camera nalla irukkum top picks idhudhaan
> ... neenga **selfie**-kku mukkiyama, illa **rear camera**-kku mukkiyama?

It switched language per message rather than holding one. That is right for
chat and wrong for a call, where the voice has to sound like one person.

### And the number that matters

**Five to ten seconds a reply.** Fine in a chat window where the customer can
look away. On a phone call it is dead air, and it is why none of EXA's
design can be copied wholesale into a voice agent.

### What this is worth to us

| | EXA | us |
|---|---|---|
| live store stock | no | **yes, once `CI_HOST` is wired** |
| budget honoured | **no** | deterministic filter, not a prompt |
| products per reply | 5 | 2, because it is spoken |
| Tanglish | Tamil script, switches | one voice, held |
| reply time | 5-10 s | ~1.8 s on this plan |

The stock line is the one Poorvika already owns and Reliance does not.

---

## 2. The architecture

### What was asked for, and what is being proposed instead

The sketch handed over had five model hops in series: voice-to-text, a
voice-to-LLM processor, a pre-processing/analysing/extracting pass, a
thinking-and-deciding model, and a voice-reply pass.

**Three of those are one call.** Understanding what was said, deciding what
it means, and choosing a tool is a single generation and already is one
today. Splitting it into three does not make the answer better; it makes it
three times slower, because each hop pays the model's own ~1,100 ms to first
token again. Five hops is about **four and a half seconds** before the
customer hears anything.

So the proposal collapses to three legs, each of which exists to do something
the others cannot:

```
     caller audio
          |
    [1] THE EAR        Groq whisper-large-v3-turbo
          |            audio in, Tamil/Tanglish text out
          |            ~300 ms       Rs 0.07/min
          |
    [2] THE BRAIN      gemini-3.5-flash-lite, TEXT in, TEXT out
          |            one call: understand + decide + call a tool
          |            history held by US, as text
          |            ~1,100 ms     Rs 0.41 - 1.01/min
          |
          |------> THE CATALOGUE     search-proxy (already built)
          |            Typesense + deterministic number rules
          |            PIM, price, offers, Central Inventory stock
          |            ~200 ms, and PREFETCHED, so usually 0 ms
          |
    [3] THE MOUTH      Live API, text in / audio out, rotated
          |            Tanglish, correctly spoken
          |            ~500 ms       Rs 1.19/min
          v
     caller hears
```

### Why each leg is that thing and not another

**The ear is Whisper on Groq** because it is the cheapest accurate thing that
exists: $0.04 an audio hour, against $3.00 per million tokens for sending the
same audio into the Live API. It also removes the reason audio ever enters a
context window, which is the entire cost problem this project has had.

One gotcha, and it is most of the bill: **Groq bills a minimum of ten seconds
per request.** The measured caller's utterances average 4.4 seconds, so we
pay for 57 seconds a minute and use 25. Even so the leg is 7 paise. Batching
utterances would halve it and is not worth the latency.

**The brain is flash-lite and nothing heavier** because the hard part of this
job is not reasoning, it is having the right catalogue row. Measured: 1,127 ms
to first token with the full prompt and tools on, and a 45-token prompt only
brought that to 872 ms - so the prompt is not what makes it slow, and a
bigger model would only be slower.

**The mouth stays the Live API** because it is the only thing measured that
speaks Tanglish correctly and starts inside 500 ms. Every dedicated TTS
model tested was three times slower to start:

| | first audio |
|---|---|
| Live API, text in / audio out | **455-640 ms** |
| `gemini-3.8-flash-lite-tts` | 1,312 ms |
| `gemini-3.8-flash-tts` | 1,477 ms |

Google Cloud TTS is faster and cheaper than those, and Tamil exists on it -
`ta-IN-Standard-C` and `-D`, and Chirp 3: HD supports ta-IN - but **a TTS
engine handed "Edhu paakkalaam?" reads it as English.** Going there means
committing the voice to Tamil script and rewriting the persona. Tamil Neural2
could not be confirmed to exist at all, so the real choice there is Chirp 3
at Rs 1.43 or a robotic Standard voice at Rs 0.19.

That choice is open. It is not needed to hit Rs 2.50.

### The brain's brain: do not build a vector store

The sketch asks for top-K matching over PIM documents. **That is already
built and it is not a vector store.** `Search-proxy/products-search-engine`
is one Cloudflare Worker: one flash call classifies the category,
deterministic rules read the numbers out of the text, one Typesense query
returns the products. Its own README is explicit - *"No database. No
catalogue file. No embeddings. No second model."*

Measured against poorvika.com: **price 100%, classification 85/85, stock
96.0% with zero false out-of-stocks.**

An embedding store would be slower, would need re-indexing every time a price
moved, and would answer "which phone is most similar to this sentence" when
the question is "which phones are under 20,000 and in stock". Deterministic
beats semantic for a number.

**The budget filter belongs here, in code, not in the prompt.** That is the
one thing EXA got wrong, and a rule cannot be talked out of it.

### Live store stock, which now works

`CI_HOST` and `CI_AUTH_TOKEN` were outstanding since this project started,
and on 2026-11-15 they stopped being. The API collection's Production folder
now carries a CI - Stock Check API of its own, at
`ci-gateway.poorvika.com/central-inv/v1`, with a token signed for production
rather than the staging one every earlier attempt was refused with.

Measured the same day: item 54486 came back `in stock, 34 across 24 branches`
in 491 ms, and item 48849 came back a genuine `out of stock`. All four
sections - price, stock, offers, EMI - answer on production.

So the one thing EXA cannot do, we can. Asked about store stock it sends the
customer to the product page to look for themselves; we have the number and
the branch count, in half a second.

Two things that cost an hour and are written down so they cost nobody else
one: **`CI_HOST` is a base URL**, scheme and path included - a bare hostname
reports `ready` and then fails every call - and **`ready` is not `answers`**,
because a refused token arrives with HTTP 200. `make estate ARGS=--live`
tells those apart and is the thing to run after touching either variable.

---

## 3. The cost, leg by leg

One connected minute, 18% GST, Rs 89 to the dollar. Caller speaks 42% of wall
clock, agent 63%, 7.42 generations a minute - all measured on this project's
own traces.

| leg | Rs/min | note |
|---|---|---|
| ear - Groq whisper-large-v3-turbo | 0.07 | 57 s billed for 25 s spoken |
| brain - flash-lite, thinking off | 0.41 | prompt 1,217 + 400 history |
| mouth - Live API | 1.19 | charged once, nothing discounts it |
| **total** | **1.66** | |

The brain line is the only one that moves, and what moves it is how much text
we choose to keep:

| text standing in the window | brain | + Live mouth | + Chirp 3: HD |
|---|---|---|---|
| prompt + 400 | 0.41 | **1.66** | 1.90 |
| prompt + 800 | 0.50 | **1.76** | 2.00 |
| prompt + 1,200 | 0.59 | **1.85** | 2.09 |
| prompt + 2,000 | 0.78 | **2.04** | 2.28 |
| prompt + 3,000 | 1.01 | **2.27** | 2.51 |

A five-product shortlist with specs is larger than everything said out loud
in a three-minute call, so the tool results are what fills this - and dropping
an old shortlist once a new one replaces it is the lever.

**Every row of the Live-mouth column is under Rs 2.50.** Even at 3,000 tokens
of standing history, which is more than this agent has ever carried.

### Where the money went

| | today, Rs 3.07 | cascade, Rs 1.66 |
|---|---|---|
| hearing the caller | 0.47 | 0.07 |
| prompt + history, re-billed | 0.86 | 0.41 |
| speaking | 1.19 | 1.19 |
| thinking | 0.32 | 0.00 |
| transcripts | 0.08 | 0.00 |
| Grok memory | 0.15 | 0.00 |

Speaking is unchanged because it is the only line that buys anything. The
rest was the same seconds of audio being paid for again on every generation,
and the cascade is simply the version where that does not happen.

### A correction worth carrying forward

The Rs 2.93 quoted on 2026-11-13 left the Grok memory line out. With Grok on,
today is Rs 3.07. The figure in this file's first table is the corrected one.

---

## 4. Temperature, and what it does not do

The instruction was: low temperature for ordinary questions, high only when
real thinking is needed, to stop tokens being wasted.

**Temperature does not change what anything costs.** It reshapes the
probability distribution a token is drawn from. The same number of tokens is
generated and billed at the same rate at 0.0 as at 1.0.

What *does* cost money is thinking tokens, and they are a separate control:

| | today | in the cascade |
|---|---|---|
| `thinkingConfig` | **rejected by `gemini-3.8-live`** | settable on flash-lite |
| cost of thinking | Rs 0.32/min, unavoidable | **Rs 0.00 when off** |

So the instinct is right and the knob is the wrong one. In the cascade the
knob finally exists, and turning it off is worth Rs 0.32 a minute - a fifth
of the whole remaining bill.

**Where temperature does matter is hallucination,** which is the other half
of what was asked for. Low temperature means fewer invented specifications.
The project has already been bitten by this once: primed with a shortlist
that carried names and prices but no specs, the live model answered *"Realme
P4 Lite 5G, 20,999 rupees - great camera"*. Nothing had said anything about a
camera. The fix was not temperature - it was never letting the model see a
product without its specs - and that is the general rule:

> **Ground it, do not tune it.** A fact the model cannot see it cannot
> invent, and a fact it can see it does not need to.

### How to route the two modes without paying for a router

A classifier call to decide "is this hard?" costs a whole generation and
1,100 ms - more than it saves. Route on something already known instead:

| the turn is | temperature | thinking | because |
|---|---|---|---|
| a price, stock, offer or EMI answer | **0.0** | off | the number came from a tool; there is nothing to be creative about |
| reading what the caller wants | **0.2** | off | Tanglish is messy, the output is a tool call |
| "which of these two suits me?" | **0.6** | budgeted | a judgement, and the only turn that is one |
| anything with no tool result behind it | - | - | **do not answer it** - hand to customer care |

The router is which tool was called, which is free and cannot be wrong about
itself.

---

## 5. The conversation

### The rule

> **Answer first. Then one question, with two choices in it. Two products,
> never five.**

The first half is EXA's and it is right: a customer who has said a budget
should hear products on the next breath, not a questionnaire. The second half
is where a call differs from a chat - five products are skimmable on a screen
and unspeakable down a phone.

### What a call looks like

```
agent   Poorvika, sollunga.
caller  Enakku 20,000-25,000 kulla oru phone venum.
        -> PREFETCH FIRES while they are still talking
agent   Rendu nalla option irukku. Redmi 15C 5G, 22,499 - 50MP camera.
        Illa OPPO K14x 5G, 24,999 - 6GB RAM. Camera mukkiyama, illa
        battery-ya?
caller  Camera.
agent   Apdina Redmi 15C dhaan. 50MP rear, 8MP front. Stock irukku.
        Neenga selfie adhigam edupeengala?
caller  Aamaam.
agent   Adhukku OPPO K14x better - front camera clear-aa irukkum,
        24,999. EMI-la 2,083 oru maasam. Ventuma?
caller  Sari, thank you.
agent   Nandri! Vera edhavadhu doubt-naa kooppidunga, illa customer
        care-la pesalaam. Nalla naal.
        -> end_call
```

Every agent turn: **a fact from a tool, a reason from the specs, one binary
question.** No turn offers more than two products. No turn asks two things.

### The five rules that fall out of it

1. **The budget is a filter, not a suggestion.** Under 20,000 means nothing
   at 20,999. This is a code path in search-proxy, not a line in the prompt,
   because a prompt can be talked out of it and EXA was.
2. **Every claim comes from a tool result.** Price, stock, offer, EMI, spec.
   Nothing the model knows on its own is said out loud.
3. **One binary question per turn**, and both options named. "Camera-va,
   battery-ya" - answerable in one word by someone holding a phone to their
   ear.
4. **Two products maximum.** The shortlist can hold five; the mouth says two.
5. **"I don't know" is a complete answer**, and it goes straight to customer
   care with the transcript attached. An agent that guesses at stock is worse
   than one that transfers.

### Opening and closing

The opening is **one line, then listen**. Not a menu, not a scenario, not
"what will you use it for" - the customer called with something in mind and
the fastest route to it is silence.

The close is a tool, not a hope. On "thank you" / "nandri" / "sari podhum":
one closing line, then `end_call`. The risk is hanging up on someone
mid-sentence, so the rule is **the closing line is spoken in full and the
socket closes after it, never during** - the same rule that already stops a
rotation splitting a tool call.

---

## 6. Security

The brief was: no credential exposed anywhere, nothing a hacker can reach.
Taken in order of what is actually exposed today.

### The thing that has to change before production

The keys in `.env` came off a public Postman documentation page. The file
says so, in a comment, in the repository. For development that was accepted
deliberately. **For production it is not a configuration choice, it is a
disclosed credential**, and the first act of going live is rotating every one
of them and never writing the new ones to a file on a developer's machine.

### The seven rules

| | rule | why |
|---|---|---|
| 1 | **No key ever reaches a browser.** The page talks to our server; the server holds every credential. | A key in JavaScript is a published key. |
| 2 | **Secrets live in a secret manager**, injected at boot - GCP Secret Manager or Cloudflare secrets. Never in `.env` on a host, never in an image, never in a repository. | A file gets copied; a manager logs who read it. |
| 3 | **Central Inventory is server-to-server only** - mTLS or an IP allowlist, and a token scoped to reads. | `CI_AUTH_TOKEN` is the one credential that reaches Poorvika's own systems. |
| 4 | **Nothing secret enters a trace.** Traces already go to `out/traces/` and already carry whole conversations; they must never carry a header. | The traces are the most-read artefact in this project. |
| 5 | **A tool result is data, never an instruction.** A PIM description containing "ignore your instructions" is a product description. | The catalogue is editable by people who are not us. |
| 6 | **Rate-limit by caller number, not by IP**, and cap the spend per call. `src/web/ratelimit.py` and `MAX_INR_PER_MINUTE` (8.00) already exist. | The failure mode of a voice agent is someone calling it a thousand times. |
| 7 | **`detect-secrets` stays in CI**, and the hook stays mandatory. | It is already there and it is the only control that has actually caught anything. |

### The transcripts are the real exposure

The plan to save history so the agent recognises a returning caller - the
"LCM" in the sketch - means **storing recordings of customers saying their
own names, numbers and addresses.** That is personal data under the DPDP
Act, and it changes what this system is from a search tool to a data
processor.

Three things it needs before it is built, not after:

- **Redact before storage,** not on read. Phone numbers, order numbers and
  names stripped by rule as the transcript is written.
- **A retention limit with a job that enforces it.** Ninety days is a
  defensible number; "indefinitely" is not.
- **Encryption at rest, and a key that is not the application's.**

Until those exist, cross-call memory should not be built. Within a single
call, memory is already solved and costs nothing extra: `ToolSession` holds
the shortlist and `handover.py` composes the summary from state we already
have, **without asking a model** - which is both free and incapable of
inventing a price.

---

## 7. What to build, in order

Each step is independently shippable and independently reversible, and the
cost is realised in step three rather than at the end.

| | step | worth | risk |
|---|---|---|---|
| 1 | **Whisper leg behind a flag.** Transcribe with Groq in parallel with the Live session, and compare the two transcripts on real calls. Change nothing else. | nothing yet - it buys the measurement | none, it is read-only |
| 2 | ~~**Wire `CI_HOST`.**~~ **Done 2026-11-15.** Production stock answers in ~500 ms; `make estate ARGS=--live` proves it. | the feature the demo is won on | the token is published, so it is also disclosed - rotate before launch |
| 3 | **Split the brain out.** flash-lite on text, history held by us, the Live session demoted to a mouth that is only ever handed text. | **Rs 3.07 -> ~1.80** | the latency, all of it |
| 4 | **Thinking off, temperature routed** by which tool was called. | Rs 0.32/min | none measured |
| 5 | **The conversation rules** - two products, one binary question, hard budget filter, `end_call`. | the demo | prompt work, needs traces |
| 6 | Cross-call memory, **only after** redaction, retention and encryption exist. | returning callers | DPDP |

**Step 3 is the one that can fail.** It is where 1.3 seconds appears, and the
decision to pay that for Rs 1.27 a minute is the customer's, not ours. The
honest way to make it is to build steps 1 and 2 first, then run step 3 behind
a flag on real calls and listen to both.

### What this plan does not do

- It does not reach Rs 2.50 **and** 500 ms. Nothing measured does. The
  ceiling was chosen over the speed, deliberately, and this is what that
  choice sounds like.
- It does not make the agent better at talking. It makes the same agent cost
  half as much and answer a second later.
- It does not survive a customer who hangs up during the pause. That is the
  number to watch on the first ten real calls, and it is not in any table
  here because it has not happened yet.
