# Getting to Rs 2 a minute without getting slower

Everything here is measured against the live API on 2026-11-10, not quoted
from a pricing page. Where a figure is a rate rather than a measurement it
says so. All rupee figures include 18% GST at Rs 89 to the dollar.

## The two constraints

- **Rs 2.00 a connected minute, everything in.** Today it is Rs 5.28.
- **Fast.** First audio after the caller stops is about 500 ms today and must
  stay near that. The agent's replies are not to be shortened.

## Why today costs Rs 5.28

The Live API re-bills the whole context window on every generation, and on a
voice call that window is mostly audio at four times the text rate. The
caller's audio and the agent's audio are charged again, and again, for as
long as they stay in the window.

| line | Rs/min | |
|---|---|---|
| caller voice + audio history | 2.64 | re-billed every generation |
| prompt + tools + text history | 1.09 | re-billed every generation |
| agent voice | 1.19 | charged once |
| thinking | 0.36 | |

Only the third line buys anything. The other three are the same seconds of
audio being paid for repeatedly.

## The move that matters

**Stop holding the conversation inside the Live API.** Send the caller's
audio to a stateless text model, keep the history ourselves as text, and the
old audio is never sent again - it is charged once, not once per generation.

    caller audio  ->  gemini-3.5-flash-lite   (audio in, text out)
                      history held here, as text
    reply text    ->  the mouth               (text in, audio out)

That one change takes the reasoning side from Rs 3.73 to **Rs 0.86** a
minute: audio at $0.30/M charged once instead of $3.00/M charged eight times.

## Choosing the mouth, by measurement

Each model was asked to speak the same 109-character Tanglish reply.
Streaming, time to the first audio byte:

| model | first audio | verdict |
|---|---|---|
| Live API, text in / audio out | **455-640 ms** | fast enough |
| `gemini-3.8-flash-lite-tts` | 1,312 ms | too slow |
| `gemini-3.8-flash-tts` | 1,477 ms | too slow |
| `gemini-3.1-flash-tts-preview` | 13,437 ms | unusable |

So within Gemini the only voice output fast enough for a phone call is the
Live API itself. The dedicated TTS models are cheaper per token and roughly
three times slower to start, and on a call the start is all that matters.

The Live API also speaks Tanglish correctly, which a conventional
synthesiser does not: a TTS engine handed "Edhu paakkalaam?" reads it as
English. Keeping the Live API as the mouth keeps the persona exactly as it
is. Going to Google Cloud TTS - cheaper and faster still - would mean
committing the voice surface to Tamil script.

### What a speaker-only session costs to run

Measured over four utterances on one session:

| utterance | first audio | prompt_audio |
|---|---|---|
| 1 | 540 ms | 222 |
| 2 | 640 ms | 428 |
| 3 | 455 ms | 607 |
| 4 | 514 ms | 718 |

Its own audio accumulates and is re-billed, exactly as in the full session.
Opening a fresh session costs **2,234 ms**, so it cannot be done between
utterances - but it can be done *during* one, since the agent speaks for
about seven seconds at a time. Keep a warm spare connection and rotate onto
it once history passes a threshold, and the re-billing stays near zero.

## Where that lands, once the reasoning leg was actually timed

The table below said the cascade would answer in 550-700 ms. That was
written before the reasoning leg was measured and it was wrong. Measured:

| | first token / audio |
|---|---|
| `gemini-3.5-flash-lite`, text in, full prompt, tools on | **1,127 ms** |
| the same with a 45-token prompt | 872 ms |
| `gemini-3.1-flash-lite` | 921 ms |
| `gemini-2.5-flash-lite` | 1,429 ms |
| the same again, over a connection held open | **1,131 ms** |

Three models, two prompt sizes, and holding the connection open changed
nothing: about a second is the model's own time to first token, not the
network and not the handshake. Compare the Live API, which understands the
caller's audio AND starts speaking in 455-640 ms.

So the cascade is:

    end of speech detected (our VAD)        ~200 ms
    flash-lite to first token             ~1,100 ms
    the mouth to first audio                ~500 ms
                                          ---------
                                          ~1,800 ms, and ~3 s when a tool
                                                    call needs two of them

Against about 500 ms today. **The cascade is cheaper and three times
slower**, and the two requirements it was meant to satisfy together - Rs 2 a
minute and a fast agent - turn out to pull against each other.

| | Rs/min | first audio |
|---|---|---|
| today: Live native audio, compression 3000/2400 | 5.28 | ~500 ms |
| **rotation**: Live, text summary instead of audio history | **2.70-3.82** | ~500 ms |
| cascade: flash-lite reasoning, Live as the mouth | 2.10 | ~1,800 ms |
| cascade with Cloud TTS Neural2 | 1.60 | ~1,550 ms, Tamil script only |
| the floor: the agent's own voice, charged once | 1.19 | — |

## The decision: not the cascade. Rotation.

Measured 2026-11-12. Two sockets, the same conversation. The first carried
its history as audio, the way the Live API does. The second was opened fresh
and handed **244 characters of plain text** instead:

> Earlier in this same call the customer asked for a camera phone between
> 20,000 and 25,000 rupees. You offered Poco M8 5G at 24,999 and Realme P4
> Lite 5G at 20,999, and they asked about the second one. Continue the call.
> Do not greet them again.

What the fresh socket then did:

| asked | answered | |
|---|---|---|
| "Adhoda price enna?" | "Realme P4 Lite 5G, 20,999 rupees." | knew which one "the second" was |
| "Naan modhalla sonna budget enna?" | "Neenga 20,000-la irundhu 25,000 rupees kulla budget sonneenga." | **remembered the budget** |
| "Modhal phone-oda camera evalavu?" | "Poco M8 5G-la 45MP camera irukku." | and the first one too |

And what it cost to carry:

| | old socket | fresh socket |
|---|---|---|
| `prompt_audio` | 778 and climbing | **222**, flat |
| `prompt_text` | 1,939 | 1,496 |
| first audio | 484-596 ms | 526-819 ms |
| connecting it | — | 1,735 ms, spent while the agent is still talking |

The budget line is the one that matters beyond cost. Compression could not
hold it: eight turns into a probe the agent had lost the 20-25k the caller
opened with, because compression drops the OLDEST turn and that is where a
budget is always said. A summary does not have a oldest turn - the budget is
in it by construction, and it stays.

### Why this wins

| | Rs/min | first audio | Tanglish | left to build |
|---|---|---|---|---|
| today | 5.28 | ~500 ms | yes | — |
| **rotation** | **2.70-3.82** | **~500 ms** | **yes** | a spare socket and a summary |
| cascade | 2.10 | ~1,800 ms | yes | a whole second voice path |
| cascade + Cloud TTS | 1.60 | ~1,550 ms | **no** | that, plus Tamil script |

The cascade is 60 paise cheaper than rotation's best case and more than a
second slower on every single turn a customer waits through. It is also the
larger build by a wide margin, and it is the only option that cannot fix the
budget bug. **Rotation.**

### Saying plainly what rotation does not do

It does not reach Rs 2.00 at this speed. Nothing measured does. Of the
Rs 2.70, Rs 1.19 is the agent's own voice - charged once, at a rate nothing
discounts, and the replies are not being shortened - and most of the rest is
the standing prompt charged on every generation. Rs 2.00 with a fast agent
is not on the table; Rs 2.70 is, and it is half of today.

### How it would work

- **The summary is written by us, not by the model.** `ToolSession` already
  holds the shortlist, and the budget is an argument the model passed to
  `search_products`. Composing four sentences from state we have is free,
  deterministic and cannot hallucinate; asking the model to summarise itself
  costs a generation and can.
- **Rotate on history, not on a timer.** Once `prompt_audio` passes a
  threshold, swap to the spare at the next turn boundary.
- **Open the spare while the agent is speaking.** 1,735 ms against about
  seven seconds of reply: it fits, and the caller never waits for it.
- **Never rotate mid-tool-call.** A function response has to follow its
  function call on the same socket - that is the 1007 that killed every call
  on 2026-11-10, arrived at from the other direction.

## What it took to build

Three files and a setting, which is most of the argument for it over the
cascade's second voice path.

| | |
|---|---|
| `src/voice/rotating.py` | the conversation across several sockets. Presents exactly the surface `Conversation` uses, so nothing above it knows |
| `src/voice/handover.py` | the few sentences that cross, composed from `ToolSession` - never asked of the model, which costs a generation and can invent a price |
| `GeminiLiveSession.prime()` | hands a fresh socket that text as context rather than as a turn to answer |
| `ROTATE_ABOVE_AUDIO_TOKENS` | 600, about twenty-four seconds of retained speech. Zero leaves one socket for the whole call, which is the behaviour it replaces |

`Conversation` now takes a `VoiceModel` protocol rather than the concrete
session. There are two implementations and neither is a special case of the
other - it is the same conversation, over one socket or several.

### Measured on the shipped path, 2026-11-12

Five caller turns through `RotatingLiveSession` against the real API, driven
the way `Conversation` drives it:

| turn | first audio | `prompt_audio` | `prompt_text` |
|---|---|---|---|
| 1 | 364 ms | 222 | 1,485 |
| 2 | 711 ms | 395 | 1,879 |
| 3 | 713 ms | 724 | 1,940 |
| | | **rotated** | |
| 4 | 789 ms | **222** | 1,542 |
| 5 | 513 ms | 329 | 1,564 |
| 6 | 607 ms | 415 | 1,599 |

The retained audio resets and starts again, which is the whole point, and
first audio stays where it was - the caller cannot tell the socket changed.
The last turn was "naan modhalla sonna budget enna?" and the answer was
"Neenga 20,000-la irunthu 25,000 rupees budget sonneenga", across the
rotation, from a socket that never heard it said.

That exchange cost **Rs 2.70** against **Rs 3.45** had the history kept
climbing on one socket: 22% off six generations, and the gap widens with
every turn after, because the thing that was growing has stopped.

### What the tests caught before a call did

A rotation that could not happen - no spare, a connection refused - left the
loop asking the session for `events()` a second time, which starts a NEW
generator and replays the call from its first turn. Forever. The fix is to
hold the stream rather than the session: breaking out of an async generator
suspends it, and iterating the same object again resumes where it stopped.

The other three worth naming, all of them quiet failures:

- Rotating on a generation that produced no audio would split a function
  call from its response across two sockets. That is the 1007 again, from
  the other direction, so a rotation only happens after the agent has
  actually spoken.
- Turn ids restart at zero on a new socket, and a consumer drops audio for
  abandoned turns *by id*. An id that went backwards would silence a turn
  still being spoken.
- The bill for a call is otherwise however much the last socket cost.

## Rates used

Per 1M tokens, read 2026-11-10: flash-lite $0.30 in (audio not surcharged on
3.5) and $2.50 out; Live $3.00 audio in, $12.00 audio out, $0.75 text in,
$4.50 text out; `gemini-3.8-flash-tts` $0.50 text in, $9.00 audio out.
Google Cloud TTS is per character: Chirp 3: HD $30/M, Neural2 $16/M,
Standard $4/M. Audio is tokenised at 25 tokens a second.
