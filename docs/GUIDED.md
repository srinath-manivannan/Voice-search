# The guided call

Written 2026-11-23 against a new brief, and every number in it was measured
that day unless it says otherwise.

**Built so far:** the whole guided call, taps and cards: the language
choice, the questions in a salesperson's order (brand, budget, what it is
for, the follow-up, the suggestion), the shelves from live stock and the
price in force, the one think, the governor, every fixed line recorded in
both languages, and the customer's own questions about the product they
picked - answered from Poorvika's own facts, checked, and capped at six.
**Not built:** spoken answers to the questions (the customer taps; the
agent talks), and the phone line. The code is `src/guided/`; this document
is why it is shaped the way it is.

## The brief, as understood

- **₹1.50 a connected minute is the target and ₹1.70, GST in, is a hard
  ceiling.** Today's Live path measures ₹2.0–3.5.
- The call stops being open voice-to-voice. The agent asks **fixed
  questions**, the customer answers by **tapping a choice** (or pressing a
  number on a phone line), and only at the end does a model think.
- The fixed questions must cost nothing. Not a cheap generation - none.
- Nobody who is wasting our time may spend our tokens. Tokens start only
  once someone has walked the questions like a customer.
- Context is paid for once, not re-sent every turn.
- The thinking step may take about five seconds behind an indicator.

That last line is the one that changes the architecture. Every earlier
attempt at cost was judged against "as fast as Live", and the cascade lost
on exactly that. Five seconds, said out loud by the owner of the product,
makes a stateless pipeline the right answer instead of the wrong one.

## Why Live costs what it costs

It is not the model's price list. It is that a Live session re-sends the
whole conversation - about 1,600 tokens of instructions, plus the audio so
far - on **every** generation, and a tool call is two generations. On the
call of 2026-11-16: 6 generations for 4 caller turns, ₹0.59 a minute for
the prompt alone, ₹0.74 in turns nobody heard.

(The dear model is `gemini-3.8-live`, native audio. `flash-lite` is the
cheap text model and is not what the voice path runs on today.)

The guided call sends context to a model **once**.

## The design in one paragraph

The call is the conversation a salesperson has, as buttons. The customer
picks a language - Tamil or English, and every sentence after that is in
it. Then: what they came for, which brand they lean to, the budget, and
**what the thing is for** - and "for studies" is followed by "studying
what?", because a school bag and an engineering course want different
phones. Out of that the call suggests what should matter most ("for
engineering: speed and 8GB RAM - shall I look for that?") and the customer
may overrule it with one tap. Nothing in that loop is a model; a question
is asked only while it can still change anything. Matching is honest about
money: products a little over the chosen budget (up to 15%) are shown only
to fill a short list and always carry "a little over your budget" - which
is a sentence a salesperson says out loud. Five or fewer matches are shown
as they are; more than five is **one** call to `gemini-3.5-flash-lite` to
pick five and say why, each reason checked against that product's own card.
Under the five: "show me cheaper" and "more like these", both free. The one
they tap is re-asked of the estate live - price, stock, offers, EMI - and
under its card are the questions customers actually ask (EMI? which shops?
warranty? other colours? what is in the box?), each answered once by the
model from a page of Poorvika's own facts and never from its memory.

```
 language ──► shelf ──► brand ──► budget ──► what for? ──► studying what? /
  buttons    buttons    buttons   buttons      buttons       what work?
     │     all of it: recorded lines + buttons, no model, ₹0, 0 ms a tap   │
     └──► "for engineering: speed and 8GB RAM. shall I?"  [ok / camera / …]
                                     │
                five or fewer left ──┴── more than five left
                       │                        │
                  shown as they are      ONE flash-lite call, ids checked,
                  (+ just-over-budget,   reasons checked, cached by answers
                   flagged, to fill)             │
                       └────────┬───────────────┘
                     up to five cards  ·  [cheaper] [more like these]
                                │ tap one
             that product, live: price · stock · offers · EMI (₹0)
                                │
         [EMI?] [which shops?] [warranty?] [colours?] [in the box?]
          six per session, each answered ONCE from Poorvika's own
          facts, every figure checked against them, cached for all
```

## The shelf is built from the estate, not from the index

This is the part the plan got wrong, and the data corrected it the first
time the real catalogue was read. The search index this service is pointed
at is **staging** (`stage-proxy.poorvika.in`, in both `devconfig` and
`prodconfig`); price and stock come from **production**. They disagree:

| | Index says | Production says |
|---|---:|---:|
| Phones in stock | 70 | **16** |
| Realme P4 Lite 5G | ₹14,499 | **₹20,999** |
| Samsung Galaxy A17 5G | ₹25,499 | **₹38,499** |
| Phones in stock, 20–30k, by index price | 7 | **0 of those 7** |
| TVs / laptops / fridges in stock | 21 / 18 / 18 | **0 / 1 / 0** |

Budget buttons built on the index offered a phone under 15,000 that sells
for 20,999, and found nothing between 20 and 30 thousand while two phones
were on the shelf.

Counted whole on 2026-11-24, because the owner asked whether data was being
lost: the index holds **12,934 records**, of which it calls **463** in
stock - the other 12,471 are listings of things the shop once sold or
accessories without live stock. Thirty of the "out of stock" were re-asked
of Central Inventory one by one: all thirty really were out, so the index's
"out" is trustworthy and nothing sellable is being hidden. Of the 463,
**371 sit on the seventeen scripted shelves** (six small ones - power
banks, chargers, cases, geysers, kitchen, home appliances - were added for
exactly this); the last 92 live under facet values the catalogue cannot
filter by at all (screen protectors, cables, memory cards).

What the big shelves really held that evening, one colour of each:
headphones 18, phones 15, fans 13, speakers 12, washing machines 4, ACs 2,
watches 2, laptops 1, and no TV, fridge or tablet at all.

So `src/guided/shelf.py` reads each shelf from the index
**once**, asks the estate about every item - stock first, then a price for
whatever is really there - and the engine narrows what survives. The counts
on the buttons are counts of products a customer can buy at that price.

It costs a burst of calls to Poorvika's own services: 86 for the phone
shelf, **5.5–6.5 s cold**. So it is done once a shelf, kept fifteen minutes
(`GUIDED_SHELF_TTL_SECONDS`), served stale while it is rebuilt, and started
when the first question is put rather than when a shelf is tapped. A tap on
a held shelf is **0 ms**. The one product a customer picks is re-asked live,
so a stale shelf can show a button that is fifteen minutes old but never a
card that is.

Three smaller things the data decided:

- **No "shall I show you?" step.** It was a tap with one answer.
- **One phone in three colours is one card.** Same model, memory and price.
- **A shelf with nothing in stock says so at once** and is then left off the
  first question, instead of being found empty four questions later.

## The questions are recorded, and each recording was listened to

Fifteen sentences - seven questions and eight things the call says - are
recorded once by `make guided-voice` and played from then on. 34 seconds of
sound, 1.6 MB, and **₹7.05 once** to make, GST in. A session plays them for
nothing.

They are spoken by the Live model through the same setup a call uses, so
they are in the voice the greeting is in. That model is a conversational
agent being asked to read a script, and the first run showed what that is
worth unchecked. Of fifteen takes:

| What was written | What was actually said |
|---|---|
| உங்களுக்கு எது முக்கியம்? | **வணக்கம்** உங்களுக்கு எது முக்கியம்? |
| எவ்வளவு கெபாசிட்டி வேணும்? | **வணக்கம்.** எவ்வளவு கெப்பாசிட்டி வேணும்? |
| எந்த சைஸ் வேணும்? | "Yeah" - the question was answered |
| இதோ முழு விவரம். | the agent declining, and introducing itself |

Each is a clip that sounds fine and is wrong, and nobody listens to fifteen
clips after every run. So the recorder does: every take is played back to
the text model, and kept only if what is heard is the line that was written
(0.90 alike on letters, which lets "பாக்கணும்" be heard as "பார்க்கணும்"
and nothing much else). Three takes, then the line goes unrecorded and is
shown in silence. Two things came out of it:

- Lines are now read with their own instruction (`read_aloud`), which says
  the line is something to read out, not respond to. The greeting's
  instruction begins "Greet the customer now", and the model obeyed it.
- What was heard is kept in `recordings/manifest.json` beside what was
  meant. A test applies the same bar to what is committed, so a recording
  cannot arrive unchecked.

**A recording is only played if it still says the sentence on screen.** The
manifest holds the text each was made from; edit a sentence in `script.py`
and its recording stops being named to the page - the line is shown, not
spoken - and a test fails until it is recorded again. The alternative is a
voice saying last month's question over this month's buttons.

In the page: a tap cuts off whatever is being said, the waiting line is
spoken only when a step takes longer than 0.9 s and is never cut short by
the answer it was waiting for, and there is a switch to turn the voice off.
A browser will not play sound on a page nobody has touched, so the first
question is silent until the first tap unless the browser already trusts
the site; the switch says so and plays it when tapped.

## What each step costs

GST in, ₹89 to the dollar. "Measured" means a real call on 2026-11-23.

| Step | Done by | Cost |
|---|---|---|
| Every question, the hold line, the result line | recorded audio | **₹0** a session |
| Making the recordings, 41 lines in two languages | the Live model, once | about ₹17 in all, measured |
| A tap, or a key on a phone | code | **₹0** |
| Choices and their counts | the shelf, read once a quarter hour | ₹0 (Poorvika's own APIs) |
| A *spoken* answer to a question | flash-lite, audio in | ₹0.005–0.015, measured, Tamil correct |
| Five or fewer left to show | code | **₹0, no model** |
| **The one think** | flash-lite, about 1,200 in / 140 out | **₹0.073, measured, 1.1–1.5 s** |
| The same think, answers seen before | cache | ₹0 |
| One question about the picked product | flash-lite over our own facts | **₹0.04–0.07, measured, 0.8–2.0 s**; same question again, anyone: ₹0 |
| Speech that cannot be recorded | flash-lite TTS, 25 tokens/s | ₹0.016/s until 2026-12-31, **₹0.032/s after** |
| One free question after the pick | STT + flash-lite + TTS | about ₹0.15, ₹0.24 from January (the text call is estimated from the rate) |

What a whole session comes to:

| Session | Total | Per minute |
|---|---:|---:|
| Web, taps, five cards | ₹0.07 | about ₹0.04 |
| Web, the customer speaks six answers | ₹0.13 | about ₹0.06 |
| …and the top pick is read aloud | ₹0.26–0.39 | ₹0.13–0.20 |
| …and three free questions follow | ₹0.70–1.11 | ₹0.20–0.32 |
| Phone line, five read aloud (carrier extra) | ₹0.47–0.86 | ₹0.19–0.34 |
| *Today, Live* | | *₹2.0–3.5* |

"Remembering the chat" goes from ₹0.59–0.75 a minute to ₹0.04 once.

Those were estimates. These are the built thing, against the live catalogue
and estate, the evening it was finished:

| Real session | Model | Cost | Per minute |
|---|---|---:|---:|
| Phone, 30–50k: four left, shown | not asked | **₹0.00** | ₹0.00 |
| Washing machine: four in stock, shown at once | not asked | **₹0.00** | ₹0.00 |
| Phone, any budget, any brand, camera: 15 left | asked once, 1.36 s, 1,348 tokens | **₹0.073** | ₹0.40 at 11 s, ₹0.17 at 27 s |
| The same answers again, new session | answered before | **₹0.00** | ₹0.00 |
| Battery instead, one second after starting | refused by the governor | **₹0.00** | ₹0.00 |
| Opening the product card (price, stock, EMI live) | not a model | ₹0.00 | 1.5–3 s |

On the shelves as they stood that day, most real paths end at five or fewer
and never reach the model at all.

## Which model, and why not the others

One call to rank twelve real phones for "camera, 25–50k", same prompt:

| Model | Time | Tokens | Cost | Verdict |
|---|---:|---|---:|---|
| `gemini-3.5-flash-lite` | 1.1–1.5 s | 1,192 in, 134 out, 0 thinking | ₹0.073 | **use this** |
| `gemini-3.5-flash` | 9.2 s | 1,559 thinking | dearer | over the five seconds |
| `grok-4.20-non-reasoning` | - | - | about ₹0.19 on its card | **the key returned `402` today** |

Speech: `gemini-3.8-flash-lite-tts` speaks Tamil in a Tamil voice
(`ta-in-assistant-4` worked), first audio in 0.9–1.2 s. Its price doubles
on 2027-01-01 - $6 to $12 a million audio tokens - which is why this
design records everything it can and caches the rest.

Hearing: flash-lite takes audio at its text rate. 1.4–2.5 s, slower than
xAI's 0.7 s, and at a paisa an utterance the difference is not worth a
second vendor.

So: one vendor, one key, one bill. xAI is not needed by anything here.

## What the measurement also said, against the plan

- **The reasons were thin.** flash-lite's first run said "nalla camera
  irukku" of every phone, and the two runs ranked differently at
  temperature 0. But all twelve candidates carried the same fact -
  `camera: 45MP+` - and the catalogue's structured specs are four keys
  (camera, memory, processor, network). A model cannot discriminate on data
  that does not. The fix is in the data and the contract, not the model:
  fetch PIM specs for the candidate set, require every reason to quote a
  value from its own candidate, reject one that does not and fall back to a
  reason built from the specs. The cache makes the answer stable.
- **flash-lite as an ear completes words** - a clip cut at two seconds came
  back as a finished word. Harmless against a closed list of choices;
  watch it in the free questions.

## Nobody spends our tokens by wasting our time

- Before the questions are finished, a tap reaches no model at all.
- A spoken answer is capped: six seconds, two tries a question, then "please
  tap one". The worst a talker can do to one question is three paise.
- The think needs a complete set of answers, the same set is a cache hit,
  and thinks are rate-limited per client.
- The questions after a pick are buttons too - EMI, shops, warranty,
  colours, the box - so there is no free text anywhere on the surface. Six
  a session, each admitted by the governor, each answered once and cached
  for everyone, and every figure in an answer must appear in the page of
  facts it was answered from or the whole answer is dropped for "I am not
  sure, ask at the shop".
- A governor sits in front of every paid call: allowed only while
  `spent + the most this call can cost ≤ ₹1.50 × minutes connected`,
  refused outright at ₹1.70, and never more than ₹3 a session. The
  questions are free, so headroom builds while they are answered and the
  ceiling holds by construction rather than by hoping. The most a think can
  cost is 21 paise, which is 8.4 seconds on the line: a customer has that
  several times over, a script has not.
- Nothing a client sends is ever a filter. An answer is the id of a button
  the engine would draw right now, or it is refused; there is no free text
  on the surface at all.

## What is already in the repository

| Have | Becomes |
|---|---|
| `voice/opening.py`, `tools/record_greeting.py --text` | every question, recorded once |
| `agent/holding.pcm` | the thinking line |
| `prefetch.read_budget` / `read_category`, `tamil_numbers` | spoken answers matched in code |
| `Catalogue`, `runner._price`, the estate | candidates, price, stock, offers, EMI |
| `cost.py` and the on-screen meter | three new lines: hearing, the think, speech |
| `ratelimit.py`, `budget.py` | the governor |
| The Live path | stays, behind a flag |

New: a script per shelf (questions as data), the wizard engine (a state
machine with no I/O), choices built from facets, the recommender (one call,
validated, cached), a speech cache, and the buttons and cards in the page.

An untracked `voice/gemini_speech.py` and its test, from another session
the same day, point the same way and do not import yet. Their two useful
ideas - flash-lite as the ear, flash-lite TTS as the mouth - are in this
plan; the files themselves are untouched.

## Build order

1. ~~The engine and the phone script, taps and cards only.~~ **Built**, and
   for every scripted shelf rather than phones alone.
2. ~~The recommender: candidates, the one call, id validation, the cache,
   and a deterministic top five.~~ **Built.**
3. ~~Record the questions; play them.~~ **Built**, in the voice the calls
   already use, with every take listened to before it is kept.
4. Spoken answers, with the caps. *(The model's dynamic answers are
   shown as text today; everything scripted is spoken.)*
5. ~~The governor at 1.50 and 1.70.~~ **Built**, with the meter on screen.
6. ~~Questions after the pick.~~ **Built**, as buttons, grounded and capped.
7. ~~Scripts for TV, AC, fridge, washing machine, laptop.~~ **Built** - and
   their questions are nearly unreachable today, because those shelves hold
   zero to four products in stock and five or fewer are simply shown. They
   lengthen by themselves on a fuller shelf.
8. The phone line: the same engine behind key presses. Waits on telephony.

## Voice both ways - built 2026-11-24

The owner's brief for the next step, in short: *after "one moment, let me
look", say what was found - "this Nokia phone, this, this" - in the voice;
and let me talk too, not only tap. If I say "the second one" or "is there
EMI?", press that button for me.*

### Where the data comes from (asked, and answered with a trace)

Nothing a customer is shown about a product is hard-coded. Traced live on
the washing machine shelf, 2026-11-24:

| Source | What it said |
|---|---|
| Search index (`stage-proxy`) | 14 washing machines listed as sellable |
| Central Inventory, per item | **10 of the 14 are out of stock** at every branch |
| PIM, per item | the price in force - e.g. ₹24,000 in the index, **₹21,990** today |
| Offers / EMI / policy / delivery | per item, on the card and in answers |

So four is what Poorvika can sell today, not a number in the code. What IS
fixed in code, on purpose, is the conversation: the question wording, the
list of shelves, the budget ladder and the purposes. That is what lets
every question cost ₹0. Brands, counts, prices, stock, specs, EMI, offers,
warranty and colours all arrive from the APIs on every shelf build or pick.

### The design

One rule keeps the cost and the abuse model intact: **the customer's voice
can only press buttons that exist.** Speech is another way to tap.

```
 customer taps the mic and speaks (stops itself after 0.8 s of silence)
        │  our playback stops the moment the mic opens - no talking over them
        ▼
 ONE flash-lite call: the audio + the buttons on screen + this shelf's
 other options  ──►  {heard, presses: [{question, choice}], free_question}
        │
        ▼
 code applies each press through engine.answer - the same NotOnOffer guard
 as a tap; anything that is not a real button is dropped
        │   "Samsung, 30 ஆயிரத்துக்குள்ள, காலேஜ் போறேன்" fills three questions
        │   "ரெண்டாவது" / "second one" picks card 2;  "EMI இருக்கா?" presses EMI
        ▼
 the pressed button flashes for 0.4 s, so the customer sees what was heard
        │   nothing matched: a recorded "புரியல, மறுபடி சொல்லுங்க / தொடுங்க"
        ▼
 the next step, exactly as if it had been tapped
```

On the card, a spoken question that is not one of the five buttons
("கேமரா நல்லா இருக்கா?") goes to the same answerer as the buttons: answered
from Poorvika's own facts, every figure checked, cached, six a session,
governed. That is the "conversation" - grounded, never free-running.

**What the agent says back, in the voice:** sentences built **by code**
from API data, never written by a model, then spoken:

- after the think: "மூணு போன் இருக்கு. ஒண்ணு: Poco M8 5G, 24,999 ரூபாய், 5500 mAh பேட்டரி. ரெண்டு: ..."
- the card: "Samsung Galaxy A56, 48,999 ரூபாய், 46 கடைல ஸ்டாக் இருக்கு, EMI மாதம் 1,389 முதல்."
- an answer from the answerer, as checked.

A model writing those sentences could misquote a price out loud with
nothing to catch it. A template cannot.

### One voice: `ta-in-techagent-5`

Chosen by the owner, by ear, from four auditions on 2026-11-24 (29, high,
"highly approachable"; it speaks Tamil and English). Every fixed line was
re-recorded in it through the speech model (`gemini-3.8-flash-lite-tts`),
and every sentence built from live data is spoken in it, so the customer
hears one speaker. A recording made in any other voice is not played.

Two things the auditions taught, both now in the code:

- **No style instruction.** "Speak softly and warmly" was read out loud on
  the English line - six words took nine seconds. The text alone.
- **A spoken price is checked before anyone hears it.** Read back, the
  auditions' "48,999 ரூபாய்" came back as 49,999 once and 48,996 once. So
  each sentence is transcribed after it is made and played only if every
  price in it is heard exactly (`speech.heard_as_said`). A sentence that
  fails is not played - the price is on screen - and is not retried per
  customer. Checked sentences are kept on disk by the hash of the voice and
  the text, so each is paid for once, for everyone.

### What it costs

STT and TTS rates were measured 2026-11-23; the hear-and-press figure was
measured live on 2026-11-24.

| Per session, three minutes, spoken throughout | Now | From 2027-01-01 |
|---|---:|---:|
| 10 spoken turns, hear + press, ₹0.05 each (measured) | ₹0.50 | ₹0.50 |
| The one think | ₹0.07 | ₹0.07 |
| Two spoken questions on the card | ₹0.13 | ₹0.13 |
| ~24 s of dynamic speech (results, card, answers) | ₹0.38 | ₹0.77 |
| **Total** | **≈ ₹1.08, ₹0.36/min** | **≈ ₹1.47, ₹0.49/min** |

Every one of those calls goes through the governor. A session that has not
earned a sentence of speech gets it as text on screen; one that has not
earned a hear gets "please tap". Spoken sentences are cached by their exact
text, so the second customer to hear "Poco M8 5G, 24,999 ரூபாய்..." costs
nothing for it.

### Nobody spends by talking

- Tap-to-talk, not an open microphone: an open mic sends room noise and our
  own voice to be transcribed, at a price, all day.
- 8 seconds a turn, 20 spoken turns a session, clips under 0.4 s never sent,
  per-client rate limits, every hear admitted by the governor.
- Speech can only press existing buttons or ask a question about the picked
  product - the same closed surface as a tap.

### Measured live, 2026-11-24

The customer's words were spoken by a different voice (the Live model) and
sent through the real ear, against the live shelves.

| Said | Ear heard | Pressed | Time |
|---|---|---|---:|
| "எந்த பிராண்டும் பரவாயில்ல, பட்ஜெட் பத்தி கவலை இல்ல, இன்ஜினியரிங் படிக்கறேன்" | the same | any brand, any budget, studies, engineering - four questions in one sentence | 2.7 s |
| "சரி, அப்படியே பாருங்க" | the same | the suggested spec (speed, 8GB RAM) | 2.6 s |
| "ரெண்டாவது போன் காட்டுங்க" | "rendavathu phone kaatunga" | card 2, re-checked live | 5.8 s |
| "இதுக்கு EMI எவ்வளவு?" | the same | the EMI question | 4.0 s |
| "இதுல சார்ஜர் பாக்ஸ்லயே வருமா?" | the same | what is in the box → "இந்த மாடலில் சார்ஜர் வராது" | 3.5 s |

**₹0.41 for 75 seconds, ₹0.33 a minute** - five things said, one answer
ranked, two questions answered. Hearing measured at about ₹0.05 a time, a
little over the estimate. Spoken sentences are not in that figure: see the
quota below.

Three things the first live run broke, all fixed and tested:

- Cards were listed to the ear without saying how to press them, and
  "rendavathu" - heard perfectly - pressed nothing. Every menu item now
  carries the press that selects it.
- "Budget pathi kavalai illa" had no button to press, and everything said
  after it in the sentence was dropped. "Any" is a menu item for every
  question now, and **answers said ahead of their question are remembered**
  and pressed when it comes up - the way a salesperson remembers.
- An EMI answer came back as the page's English lines with one Tamil word
  between them, and passed a "three Tamil letters" check. Language is now
  judged by the share of Tamil letters (30% or more), and the instruction
  says to say the facts in Tamil, not copy them.

### The speech model allows 100 requests a day

Found 2026-11-24, by running into it: `gemini-3.8-flash-lite-tts` is limited
on this project to **100 requests per day** (`GenerateRequestsPerDayPerProjectPerModel`).
The auditions and the re-recording spent it. Until it resets:

- 33 of the 47 fixed lines are recorded in the chosen voice; the other 14
  are shown without sound, by design, rather than played in the old voice.
  `make guided-voice` records the rest once the quota resets, and four
  tests in `test_guided_voice.py` stay red until it has.
- Spoken results, cards and answers are refused by the voice and shown as
  text - the fallback working as intended.

100 a day is a few dozen sessions. **Serving customers needs a higher
quota** on the Google project - a billing-tier question for the owner, not
something code can fix.

### After the owner's first real test, 2026-11-24

Four things the owner's own hands found, all changed:

- **The call now answers like a shop**: "வணக்கம், பூர்விகாவுக்கு
  வரவேற்கிறோம்!" opens it, and speaking to that first screen in either
  language both sets the language and, if a product was named, walks
  straight to it - "enakku phone paakanum" needs no taps at all.
- **Voice mode stays on.** The mic button is a switch, not a push-to-talk:
  tapped once, the call listens between every step, abandons anything it
  overhears while the agent is speaking or a step is running, and sends a
  stretch of speech only after 0.8 s of silence ends it. Tapped again, off.
- **Nothing cuts dead any more.** An interrupted line fades over a fifth of
  a second instead of stopping mid-word ("டக்குன்னு கட்").
- **The meter starts with the first answer**, not when the page opens: a
  welcome screen left open is nobody "on call".

### The owner's second test, 2026-11-24: the salesperson's script, whole

Dictated turn by turn and built the same day, all free - every piece is
fixed lines, buttons and the list in memory being re-cut:

- **The conversation goes as deep as a salesperson's.** "For work" → "what
  kind of work?" → "what kind of IT work?" → the suggestion ("for a
  developer: speed, 8GB RAM, plenty of storage - shall I look for that?").
  Entertainment splits into games, photos and movies. Three levels, every
  question a recorded line, every answer a button or a word.
- **Over budget is a question now, not a surprise.** When the band runs
  short and something sits a little above it: "உங்க பட்ஜெட்டுக்குள்ள அதிகம்
  இல்ல. கொஞ்சம் மேல போனாலும் காட்டட்டுமா?" - with the counts on the two
  buttons. Nothing above the budget is shown until the customer says yes,
  and what is shown still carries the flag.
- **Fours, then "show me more".** The top four, and "இன்னும் காட்டு" adds
  the next four under them, again and again; when the shelf is out the call
  says so - "ஸ்டாக்ல இருக்கறது அவ்வளவு தான்" - and the whole list stays on
  screen to choose from.
- **A brand named out loud that is not in stock gets an honest reply**:
  "அது இப்போ ஸ்டாக்ல இல்ல" and what IS in stock stays on screen, instead of
  a blank "say that again".
- **The sale ends with a person.** Under every card: "கடை ஆள்கிட்ட
  பேசணும்" - and the call names the shops actually holding the piece, from
  Central Inventory, best-stocked first. A real transfer needs telephony;
  until then the customer leaves knowing which counter to walk to.

Also that day, from the owner watching the page: the meter no longer ticks
on the welcome screen (it starts with the first answer), an interrupted
line fades for a fifth of a second instead of cutting dead, and the mic
became voice mode - one tap and the call listens between every step, so
nobody pushes a button to talk.

### The owner's third test, 2026-11-24 evening: simpler

With the day's voice quota already spent, the agent could not say its new
lines - and the open microphone, with nothing talking back, pressed
buttons he had not meant. His verdict: not MNC-grade, too complicated;
**"நான் click பண்றேன், அது பேசணும்"** - he taps, it talks. So:

- **The mic is off by default** (`GUIDED_MIC=1` brings it back). The whole
  hearing stack stays built and tested; it is the phone line's ear.
- The page speaks every recorded line and every checked sentence; fades
  are a quarter-second cosine now, and a line replayed during its own
  fade-out no longer gets paused by it.
- **Shelves are built at server startup**, so the first tap of the day no
  longer costs its customer six cold seconds.
- A waiter probes the voice quota every half hour and records the missing
  lines the moment it resets; if the machine restarts first, one
  `make guided-voice` does the same.

The target he restated: **under ₹1 a minute**. Tap-and-talk measures
₹0.10-0.40, so the room is there.

### The static, found and fixed - 2026-11-24 night

What the owner heard as "TV signal போன மாதிரி குறுக்கு" was not the voice:
the speech model answers `generateContent` with `audio/wav`, and
`Speaker.say` wrapped that - header and all - inside our own WAV. **Every
techagent-5 recording carried a second 44-byte RIFF header in its first
samples**, played as a click, with a mis-declared length behind it. The
read-back never caught it because the transcriber shrugs off two
milliseconds of noise. Fixed twice over: `speech.pcm_from` now strips
embedded headers from everything the model sends, and all 33 damaged
recordings were repaired in place from their own bytes - no quota, 17
paise of re-verification.

While the day's voice quota blocks techagent-5, lines it has not yet
recorded are spoken by an honestly-tagged **stand-in** (the Live model's
reader voice): a line said by the other speaker beats a line said by
nobody, which is what the owner was hearing. `Recordings` plays a
stand-in only while no right-voice file exists, logs it, and the
overnight waiter replaces the lot. One line (`ask_fun`) failed its
read-back three times and stays text-only until then.

### Which model, asked directly - the pricing page, read 2026-11-24

The owner asked whether another model would make the open voice call
cheap: a different Gemini, Grok, or something tuned. The pricing page
(aistudio.google.com/docs/pricing), read that night:

| Live (speech-to-speech) model | In text/audio | Out text/audio |
|---|---|---|
| gemini-3.8-live | $0.75 / $3.00 | $4.50 / **$12.00** |
| gemini-3.1-flash-live-preview | $0.75 / $3.00 | $4.50 / **$12.00** |
| gemini-2.5-flash-native-audio* | $0.50 / $3.00 | $2.00 / **$12.00** |

*from the page's unverbatim tail; the rest read verbatim.*

**Every live model speaks at $12 a million tokens and re-bills its context
per turn** - the two things that put the Voice Call tab at ₹2-3.5 a
minute. Swapping it cannot reach ₹1.50. Grok has no live audio model, its
text card ($1.25/$2.50) is dearer than flash-lite ($0.30/$2.50), and the
key returns 402. Fine-tuning does not exist for live models and never
lowers a price card. The cheap road is the one already built: recorded
lines and flash-lite's ear, brain and mouth - ₹0.10-0.46 a minute
measured. If an OPEN conversation is ever wanted at that price, it is the
same stack with free speech instead of buttons, at ~₹0.5-0.9 a minute and
two-to-four-second replies. Worth knowing: the TTS **Batch tier is half
price** ($3/M until January), which would halve the cost of recording
sessions that can wait a minute.

The Live tab's target now reads the owner's ₹1.50 (`TARGET_INR_PER_MINUTE`),
so its meter shows the truth: that path runs red.

### The owner's fourth walk, 2026-11-24 night: a consultation, not a counter

He tried the speaker shelf: brand tapped, products thrown at him. "அப்படி
பண்ணக்கூடாது - step by step refine பண்ணி, கடைசியா தான் product எடுத்து
கொடுக்கணும்." Four changes, all still ₹0:

- **The ladder is never skipped.** Every question is asked however thin
  the stock - the old "five or fewer, just show them" shortcut sold the
  consultation short, and it is gone. A speaker is asked "எதுக்காக, எந்த
  இடத்துக்கு?" (room / hall with the TV / party), a TV which room, a
  headset what for - his own examples, now scripted with their own
  recorded lines.
- **ONE suggestion at a time.** The think ranks once; the customer is
  handed one product - "உங்க தேவைக்கு இது நல்லா பொருந்தும்னு நினைக்கிறேன்.
  இது சரியா, வேற பாக்கட்டுமா?" - and "வேற ஒண்ணு காட்டு" walks to the
  next, exactly as he narrated. "லிஸ்ட்டா காட்டு" lays out **ten** to
  choose from, "இன்னும் காட்டு" extends it, and the end of the shelf is
  said honestly.
- **A brand the shelf no longer holds is said out loud** - "அந்த
  பிராண்ட்ல இந்த பட்ஜெட்டுக்குள்ள இப்போ எதுவும் இல்ல" - the budget stays,
  the brand question returns with honest counts. By tap this cannot even
  arise (the counts prevent it); it guards the voice path and stock that
  moves under a session.
- Verified live on the speaker shelf end to end; 13 new lines stood in by
  the Live voice tonight, replaced by techagent-5 when the quota resets.

### The zero-paisa voice conversation - 2026-11-24, last light

The owner's final shape for the voice product, in his words: the questions
by voice, the answers by voice, **"ஒரு பைசா கூட செலவாகக்கூடாது"** for the
whole of it; the answers saved, turned into one meaningful prompt, handed
to Gemini once at the end - and only there does the model exist.

Built exactly so, because the browser closes the one gap money was going
to: **Chrome's own speech recognition hears the customer for free.** The
words go to `/guided/said`, and `src/guided/match.py` - string work, shop
vocabulary, the Tamil number reader - presses the buttons they mean. It
can only press buttons that exist, so the question phase cannot reach a
model AT ALL before a product is picked; the ladder's structured answers
become the one think's prompt, unchanged from before.

What one breath does, verified live at ₹0.00: "சாம்சங்ல
முப்பதாயிரத்துக்குள்ள காலேஜ் படிக்கறதுக்கு ஒரு போன் வேணும்" sets the
language (from the language it was spoken in), the shelf, the brand, the
budget band ("க்குள்ள" read as a ceiling: 30,000 ends its band) and the
purpose chain, and leaves the call standing on the spec suggestion. The
matcher also carries ordinals ("ரெண்டாவது"), the card questions, the
commands ("வேற", "லிஸ்ட்டா", "கம்மி விலைல", "முதல்ல இருந்து"), "சரி" as
accepting whatever suggestion is on screen, and colloquial money from
"பதினைஞ்சாயிரம்" to "ஒன்றரை லட்சம்" to "thirty thousand" to "25k".

A spoken clash is handled the owner's way: "Samsung, under 15 thousand"
where Samsung starts at 16,000 drops the brand, SAYS the recorded clash
line, keeps the budget remembered, and presses it by itself the moment a
brand that fits is chosen.

The conversation carries itself: after every line the agent finishes, the
browser listens again - one tap on "பேசி பதில் சொல்லலாம்" starts it, one
tap ends it. The paid ear (`hear.py`, flash-lite) now exists only for the
phone line, where there is no browser to listen for free.

### The production spec, adopted and deferred - 2026-11-24

The owner pasted a three-part spec. What it asked for, and where each piece
stands. "Adopted" means built and tested; nothing below is claimed beyond that.

**Adopted**

- The spec's greeting, word for word, recorded in techagent-5 (4.8s, under
  the 6s bound the first draft missed at 7.5s).
- The read-back before any recommendation: a fixed recorded line ("நீங்க
  சொன்னது இது. சரியா, ஏதாவது மாத்தணுமா?") over a summary composed only
  from the labels on the buttons the customer chose. `engine.CONFIRM`.
- Corrections at the read-back, by voice: every question re-opens, the
  newest word wins, the kept answers replay and the read-back comes again
  with the new summary. "மாத்தணும்" with nothing named steps back one.
- Four cards on screen, the best one spoken; "இன்னும் காட்டு" the next four;
  "பத்து காட்டு" ten; walking off the end lays the list out and says so.
- Spoken budgets kept exact ("cap:30000", "₹30,000 வரை") instead of being
  rounded to a band.
- Catalogue pagination past 120 per page, stopping on a stalled page; and the
  four counts kept apart per shelf - records, in stock, priced, models -
  plus "matching" for the session, in the developer drawer.
- The governor at stricter defaults than the owner's lines: ₹1.00 target and
  ceiling, ₹0.25 a call on models, and only *active* seconds earn room - an
  idle tab earns nothing.
- One paid model call per call: the ranking think, and only when it is a
  judgement. A priority with a number behind it (camera MP, battery mAh,
  RAM, storage, stabilisation) is ranked by that number for nothing; only
  "value for money" reaches the model.
- One voice. A line with no recording in techagent-5 is shown, never
  spoken by another voice: the espeak route and the device-voice fallback
  are gone, and stand-in takes are no longer played (owner, 2026-11-24:
  "vera men voice" was improper).

- Two faults the owner hit on the same evening, fixed: tapping the third
  purpose level (work -> IT -> role, asked as `use2`) was refused by a
  letters-only request pattern; and on a machine with no on-device Tamil
  pack the mic went to a local Whisper worker that was not installed, so
  nothing was heard. The browser's own recognition (free to us) now comes
  before the worker. `say_result` is retired - three takes came back as
  "touch YOU" or "start", and its only caller replaced it anyway.
- The owner's voice test, later that evening: the agent cut itself
  mid-word ("நோக்-") and looped "மன்னிக்கணும் புரியல". Cause: the mic was
  opened as each line STARTED, so the recogniser heard the agent through
  the speakers, its speech-start hushed the agent, and its own words came
  back as the customer's. Now half-duplex - the mic opens 350ms after the
  last line ends and closes when any line starts; speech never cuts the
  agent (a tap still does, with the fade); the sorry line is said once per
  question and later misses are only shown.
- Same test, a second fault: "எந்த பிராண்டும் பரவாயில்ல" pressed budget=any
  as well, the spoken budget after it was ignored, and the think ran
  (₹0.074) for want of a budget. An "any" word now belongs to the nearest
  brand or budget word before it (after it only in English order), else to
  the question on screen. Speaking itself is counted in the meter
  (`spoken_turns`) and costs ₹0 - a test speaks a whole call and asserts it.
- One-off recording cost for the evening: about ₹4.80 incl. GST for 50
  lines re-made in techagent-5. Not a per-call cost.

**Deferred, with the reason**

- *Product sentences spoken.* The result line naming the product and its
  price needs the paid TTS (about ₹0.10 a result at today's rate, doubling
  on 2027-01-01). It is a second paid call against the spec's "one". The
  owner chose, 2026-11-24: keep it silent - the cards are read on screen.
- *Card questions the card cannot answer* (warranty, what is in the box)
  go to the shop, not a model, for the same reason. EMI, branches, offers,
  colours, price and "why" are answered free from the card.
- *The phone line's paid ear* gets about six turns inside ₹0.25 before it
  asks for taps; the browser's free ear has no limit.
- PostgreSQL persistence is written (`store.py`, `GUIDED_DATABASE_URL`) but
  not run against a database here; without one it keeps sessions in memory.
- whisper.cpp / faster-whisper fallback is written (`local_speech.py`) and
  needs the model files on the host; not exercised here.
- CDN for recordings, the 4-stage rollout and the p95 targets are operations
  work with no host to measure on yet.
- Grok 4.3 against flash-lite: the xAI key answers HTTP 402, so there is
  nothing to benchmark until it is funded.
- Hard brand constraint vs brand preference, and "சுமார் ₹25,000" read as a
  band around the figure rather than a ceiling: not built.
- The acceptance journeys - a full Tamil phone call and an English speaker -
  need a real microphone and a person; the owner's walk is that test.

### Phase 1, the live catalogue, and what a journey really costs - 2026-11-24

- **Typing on the Guided screen.** A box, "அனுப்பு" and the mic sit under
  every screen from the greeting on (`#g-compose`). Typed words take the
  same free path as speech (`POST /api/v1/guided/said`, `kind: "text"`) and
  come back with an `overview` - "சரிங்க, Samsung போன் பாக்குறீங்க." -
  above the next question, with a recorded "சரிங்க." before it. The
  overview is built only from answers that took (`engine.overview`), so a
  brand dropped for the budget is never claimed. Both of the owner's
  examples pass word for word (`tests/test_guided_overview.py`); the one
  verb is kept as பாக்குறீங்க in both.
- **Site video.** "videos at construction sites" is its own use under work,
  asked "நடந்து கொண்டே வீடியோ எடுப்பீங்களா, ஒரே இடத்திலிருந்து
  எடுப்பீங்களா?" - walking points the ranking at stabilisation the spec
  sheet proves, a fixed spot at the camera.
- **A yes is not a brand.** A bare "சரி/ஓகே" presses the first button only
  where it is an agreement (spec, read-back, budget-up); "எந்த பிராண்டும்
  ஓகே" is any brand. Live, it had pressed Nokia.
- **The catalogue is production now** (`CATALOGUE_*` in `.env`, staging
  kept commented): POST to the proxy's `/typesense` root, key in
  `X-TYPESENSE-API-KEY` (`CATALOGUE_KEY_HEADER`), Origin
  `https://search-proxy.poorvika.in`, a named User-Agent for Cloudflare.
  19,057 records in `productsnew` (staging had 12,934). Phone shelf: 635
  index-in-stock -> 630 really in stock -> 630 priced -> 275 models
  (staging: 71/17/17/16); six Samsung models at or under ₹25,000. The
  phone shelf takes about three minutes to build after a start (one
  estate check per item); it is warmed at startup and served stale while
  it rebuilds.
- **Measured cost of one complete journey** (to card, EMI and shops), real
  rates with GST: ₹0.00 when rules decide; ₹0.058 when the one think runs
  (614 tokens, 4.4 s); asking again ₹0.00. A typed turn takes 4-5 ms on a
  warm shelf. The Voice Call tab (Gemini Live) is a different product:
  ₹0.47 a minute of the customer talking, ₹1.9 a minute of the agent
  talking, plus the chat re-billed each turn - it measured ₹3.40 a minute.

### The Voice Call tab stays Live, for demos - 2026-11-24

The owner first chose the free Guided voice for that tab, then kept Live
for tomorrow's demo: Voice Call is the Live conversation again (₹3.40 a
minute measured), Guided is the product being taken forward. `LIVE_VOICE=0`
switches Live off - `/ws` then refuses at the handshake and "Start Call"
opens Guided with the mic on (`startGuidedVoice` in `guided.js`). "English"
said into the Tamil recogniser ("இங்கிலீஷ்") is read as English - the
language named wins over the script it arrived in.

### Faster, all over - 2026-11-24 night

The owner saw "ஒரு நிமிஷம், பாக்குறேன்" after tapping a shelf. On the
production catalogue the phone shelf is 630 items checked one by one
against the estate (Central Inventory has no bulk call) - about three
minutes. Measured, then fixed:

- **Nobody waits for a build.** Each built shelf is saved to
  `out/guided-shelves/<shelf>.json` (gitignored, never older than six hours
  when served) and served at once after a restart while it is rebuilt. A
  first-ever build waits 1.5 s for the estate, then lets the questions run
  on the index's own shelf (every item unconfirmed); only the
  recommendation waits for verified items. An outage - most prices
  unanswered - fails the build instead of saving an empty shelf (this
  happened once, live: seventeen shelves built at once, all timed out).
- **One shelf at a time** at startup, every shelf, phone and speaker first.
- **The card is fetched before the tap.** When cards appear, their live
  price, stock, offers and EMI are asked for at once (Poorvika's own APIs,
  free); the tap joins that ask. Background builds take 8 of the estate's
  24 connections, so a tap never queues behind them.

| Step | Before | After |
|---|---|---|
| Tap a shelf, cold server | up to 3 min | 3-6 ms |
| Typed sentence / recommendation | - | 4-10 ms |
| Card tap after reading the cards | 2.5-6.5 s | 8-11 ms |
| Card tap 0.8 s after they appear | ~4 s | 325 ms |

Production shelves now: phone 275 models, TV 136, fridge 123, washing
machine 106, headphones 91, AC 76, speaker 75, laptop 70, fan 64, case 51,
watch 48, geyser 31, tablet 27, charger 22, power bank 21.

### The review's quick fixes, done - 2026-11-24 night

- **Dealer by voice.** "டீலர்கிட்ட பேசணும்" was heard and dropped (it is not
  one of the suggested card questions); it now reaches the dealer step.
- **The think is for verified accounts only**, as the owner's spec says.
  A guest gets the whole flow ranked by rule at ₹0. (Earlier the same day
  it had been opened to confirmed guests; that is reverted.)
- **Turned-down cards stay down.** "இதுல எதுவும் வேண்டாம், வேற காட்டு"
  remembers the four on screen; the list, "இன்னும்" and "cheaper" leave
  them out until a new ranking is made.
- **"Samsung கட்டாயமில்லை" is any brand.** A not-required word right after
  a brand takes it back; at the read-back the rest of the need is kept and
  read back again. (Excluding one brand is not modelled yet.)
- **Cards A–D.** Each card carries a letter; "C option-க்கு EMI இருக்கா?"
  picks the third card ON SCREEN (after "வேற காட்டு" that is #7) and answers
  its EMI under the card - ₹383 x 48, HDFC, matching the estate.
- **Names.** A slug for a name is rebuilt from `model` + `option_value`.
  Production has none today; staging did.
- **A tap asks again if the early ask came back empty** - a busy estate
  once left cards with no live price, stock or EMI. Restored shelves keep
  their real age, so a restart no longer sets off fifteen rebuilds.

Still open: spoken overviews (slot clips), the "relax one condition" offer
when cards run out, USB-C and other feature requirements (needs a PIM
field), excluding one brand, the Chat tab's uncapped model calls, and real
microphone tests in Chrome and Safari.

### The inch-by-inch audit - 2026-11-24, last

The owner asked whether everything asked for over the day was done and
every credential integrated. A fresh sweep of 24 checks on the live server
found four real faults behind passing tests, all fixed and re-run (24/24):

- **Keypad phones as answers.** "Site video, walking, ₹25,000" showed three
  ₹1,399-₹1,899 keypad phones. Phones now carry `kind` from the index's
  `keypad-mobile` slug; keypads are left out above a ₹5,000 ceiling
  (`engine.KEYPAD_BUDGET`). Shelf snapshot version 2.
- **Apple refused.** A said or typed brand was accepted only if it was one
  of the eight buttons; Apple had stock and was not among them. Any brand
  the shelf really holds is now taken.
- **English overview without the budget.** "under 20000 for my mother":
  the budget waits for its question (brand first) and was left out of the
  overview; remembered answers are now said back. "mother/father/parents/
  பாட்டி/தாத்தா" are family use.
- **Profession inference.** The engineering course and developer leaves
  presumed "speed, 8GB RAM"; they now ask "உங்களுக்கு எது முக்கியம்?" with
  nothing pre-picked. Uses that describe what the customer does (photos,
  games, site video) still suggest.

Credentials, tested live (status only): Gemini 200; production Typesense
200 (19,057); estate in production - PIM, pricing, Central Inventory, EMI
ok, offers ok on 29 of 40 phones (empty on the rest); xAI lists models but
a one-token call answers 402, out of credits. Cloudflare, CF Access, the
demo-shop admin login and the Worker gate tokens are not used by this app:
the estate runs in-process.

### The open list, finished where it could be - 2026-11-24, night

Built, tested, and run in the 50-scenario gate (`python -m
src.tools.guided_scenarios`, 50/50 on production data, ₹0 every one):

- **Brand-budget clash is a choice.** "Samsung under ₹15,000" with nothing
  there asks "வேற பிராண்டா, அதே பிராண்டா?" - another brand at this budget,
  or Samsung from the next price it holds (the figure is on screen).
- **Brief model.** Every response carries `brief`: `must` (budget, brand,
  features, not_brands), `prefer` (brands, use), `priority`, `unknown`. The
  read-back says the same in words.
- **Exclude and prefer a brand.** "Samsung வேண்டாம் / தவிர / no Samsung"
  takes Samsung off; "Vivo இருந்தா நல்லது / preferably Vivo" ranks Vivo
  first and hides nothing. "Samsung கட்டாயமில்லை" is still any brand.
- **"சுமார் ₹25,000"** is ₹22,500 - ₹27,500, not a ceiling.
- **Feature needs.** USB-C, 5G and AMOLED come from the PIM sheet only
  (production: USB-C confirmed on 243 of 275 phones, 5G on 228, AMOLED on
  132); a must keeps only confirmed phones.
- **When the cards run out** the call asks what to change, each offer with
  how many more it finds: a higher ceiling, any brand, without a feature,
  a left-out brand back in.
- **Directions for every shop** in the dealer handover (the estate has no
  phone numbers, so a real call transfer cannot be made from this data).
- **Chat tab cap.** `CHAT_SESSION_CAP_INR` (₹1.00 by default): past it, no
  model call, and the way to Guided is said.
- **Barge-in, opt-in.** `?bargein=1`: an echo-cancelled mic watches while
  the agent talks; 300 ms of loud speech fades it out. Off by default
  until tested on a real phone - the half-duplex default stays.

Needs the owner, not code:
- local speech (faster-whisper "small", about 460 MB model download);
- PostgreSQL (none on this machine; the store and driver are ready -
  `GUIDED_DATABASE_URL`);
- a CDN for the recordings (Cloudflare publishing);
- Android Chrome and iPhone Safari mic tests (a person and two phones);
- Grok comparison (the xAI account is out of credits);
- a real dealer call (a telephony provider and shop numbers);
- spoken overviews from recorded slot clips (about 130 recordings, two
  days of the TTS quota) - or paid TTS per overview, the owner's call.

### Where it is in the code

| Piece | File |
|---|---|
| The voice, the checked cache, the date-aware rate | `src/guided/speech.py` |
| The sentences built from live data | `src/guided/spoken.py` |
| The ear (transcribe; read-back) | `src/guided/listen.py` |
| Speech into presses: the one call | `src/guided/hear.py` |
| Presses applied as taps, the menu, the caps | `Flow.hear` in `src/guided/flow.py` |
| Tap-to-talk, silence detection, the visible press | `src/web/static/guided.js` |

Still to build: the phone line, with key presses as the taps, and a
real dealer transfer on it.

## Decisions that are the owner's

1. **The voice.** The recordings are in the voice the calls use today -
   which is the API's default, because `VOICE_NAME` is empty. That is a
   choice nobody made, and an unpinned default can change under us: the
   recordings would stay as they are while live speech moved. Pin it. To
   change it, set `VOICE_NAME` and run `make guided-voice ARGS=--all`
   (about ₹4-7) and `make greeting`.
2. **Speaking during the questions.** Taps only today, and the shape no
   longer needs the customer's voice at all: even "any other questions?"
   is buttons. Adding spoken answers back is build-order step 4 and waits
   on this being wanted.
3. **xAI.** The key returns `402`. Nothing here needs it.
4. **Which catalogue production reads.** Both config files point the search
   index at staging. The guided call is correct either way, because it
   believes the estate and not the index - but it can only offer what the
   index lists, and a staging index lists 70 phones. Pointing it at the
   production index is a setting (`CATALOGUE_URL` and its token), and it is
   the single change that would put more than sixteen phones on the shelf.
5. **The one voice.** Voice both ways needs a TTS voice for the dynamic
   sentences, and the fixed lines re-recorded in it. Which voice is the
   owner's to hear and choose.
6. **Out-of-stock products.** Today they are not shown - 10 of 14 washing
   machines, 54 of 70 phones. Showing them as "இப்போ இல்ல" would make
   shelves look fuller and sell nothing today.
7. **The load on Poorvika's services.** Building all eleven scripted
   shelves is about 330 calls and 44 seconds, one shelf at a time, once -
   and then one shelf again when it is next asked for after fifteen
   minutes. That is the price of true buttons. The interval is
   `GUIDED_SHELF_TTL_SECONDS`.
