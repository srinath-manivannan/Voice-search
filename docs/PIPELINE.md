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

## Where that lands

| | Rs/min | first audio |
|---|---|---|
| today: Live native audio end to end | 5.28 | ~500 ms |
| reasoning on flash-lite, mouth on Live | **~2.1** | ~550-700 ms |
| the same with Cloud TTS Neural2 | ~1.6 | ~250 ms, Tamil script only |

**About Rs 2.1, not comfortably under Rs 2.00.** The agent's voice alone is
Rs 1.19 of it and the replies are not being cut, so the remaining room is in
how many generations a turn takes - 1.38 today, 1.20 would pay for the rest -
and in how much of the call is filler rather than answer. The agent speaks
63% of the wall clock; at 50% the figure is Rs 1.85, and that is trimming
padding, not answers.

## What this costs to build

Not a setting. A second voice path, behind a config flag, reusing what is
already here:

- `src/voice/vad.py` and the microphone gate already do endpointing.
- `src/chat/gemini_text.py` already runs flash-lite with tool calling.
- `src/agent/tools/runner.py` is already shared by both surfaces.

What is new: a speaker pool over the Live API with warm-spare rotation,
barge-in against it, and a turn loop that hands audio to flash-lite and text
to the speaker. Latency has to be measured on a real call before it ships,
against the Phase 1 targets of 1 s p50 and 2 s p95.

## Rates used

Per 1M tokens, read 2026-11-10: flash-lite $0.30 in (audio not surcharged on
3.5) and $2.50 out; Live $3.00 audio in, $12.00 audio out, $0.75 text in,
$4.50 text out; `gemini-3.8-flash-tts` $0.50 text in, $9.00 audio out.
Google Cloud TTS is per character: Chirp 3: HD $30/M, Neural2 $16/M,
Standard $4/M. Audio is tokenised at 25 tokens a second.
