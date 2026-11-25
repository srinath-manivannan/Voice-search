# Going live with no database and no speech server

The owner's plan for the first public release (2026-11-25): every user on
the site, the smallest server that works, no database for now, no speech
model of our own - third-party APIs only. This is what that looks like, what
it costs, and what has to be true before the switch is flipped.

## What runs

One container, one process (`uvicorn`, a single worker - the `Dockerfile`
default). Everything a customer has open lives in that process's memory.

| Piece | Where it comes from | Cost per use |
|---|---|---|
| Guided questions, overview, cards | this process (rules, recorded voice) | ₹0 |
| Hearing the customer | the browser's own speech recognition (Chrome, Edge, Safari) | ₹0 |
| The agent's voice in Guided | recordings in the image (`src/guided/recordings`) | ₹0 |
| Catalogue | production Typesense via `proxy.poorvika.com` | ₹0 (Poorvika's) |
| Stock, price, offers, EMI | the estate, run in-process from `vendor/estate` | ₹0 (Poorvika's) |
| Ranking think | Gemini text, verified accounts only, inside the daily ceiling | ~₹0.06 when used |
| Voice Call tab | Gemini Live, inside per-call and daily ceilings | ~₹3.40 a minute |
| Chat tab | Gemini text, ₹1 per conversation, inside the daily ceiling | a few paise a message |

Not used, and not needed: a database (`GUIDED_DATABASE_URL` empty - sessions
are in memory), a speech server (`GUIDED_ASR_URL` empty - the mic is offered
only where the browser hears; elsewhere customers tap and type), runtime
text-to-speech, the xAI key.

## The one rule that comes from "no database"

**Exactly one instance.** Sessions and the daily ceiling live in memory, so a
second instance would not know the first one's customers. Set minimum and
maximum instances to 1 on whatever host runs it. One process is I/O-bound
and should hold hundreds of concurrent Guided sessions; measure before a
campaign. Scaling past one instance is the day a database (or sticky
routing) becomes necessary.

What a restart or redeploy costs without a database: customers mid-journey
start again, and the day's spend count starts again (it can only let a
little more through, never block). Shelves are rebuilt in about three
minutes - questions keep working meanwhile on the index's own shelf, and
`/readyz` reports `"warm": true` once recommendations will not wait. A small
persistent volume at `/app/out/guided-shelves` makes restarts instant.

## Money: the ceilings to set

Guided is ₹0 for everyone. The paid tabs are bounded three ways:

| Setting | Default | For a public site |
|---|---|---|
| `DAILY_SPEND_CAP_INR` | 300 | everything paid, all users, per day (IST). Past it, Voice Call and Chat pause until midnight with a message; Guided carries on |
| `MAX_SESSION_INR` | 50 | **10** - one Live call stops at ₹10 (about three minutes) |
| `MAX_SESSION_MINUTES` | 10 | 5 |
| `CHAT_SESSION_CAP_INR` | 1.00 | 1.00 |
| `LIVE_VOICE` | on | on while demos need it; `0` takes the paid Voice Call off entirely and the tab opens Guided by voice |

With the defaults above, the worst day costs ₹300 of model spend whatever
the traffic.

## Server

Any host that gives HTTPS and WebSockets: Cloud Run, Render, Railway, or a
small VM behind Caddy. HTTPS is not optional - browsers only open the
microphone on a secure page. A 2 vCPU / 2-4 GB machine is enough for one
process; roughly ₹1,500-3,000 a month depending on provider (check the
provider's own price page - this is an estimate).

If the host has request timeouts, Voice Call needs WebSockets held for the
length of a call (up to `MAX_SESSION_MINUTES`).

## Environment (names only - values go in the host's secret store)

Required: `GEMINI_API_KEY`, `CATALOGUE_URL`, `CATALOGUE_TOKEN`,
`CATALOGUE_ORIGIN`, `CATALOGUE_KEY_HEADER=X-TYPESENSE-API-KEY`,
`ESTATE_BACKEND=inprocess`, `ESTATE_API_ENV=production`,
`PIM_ACCESS_TOKEN`, `OFFERS_API_KEY`, `CI_HOST`, `CI_AUTH_TOKEN`, `CI_MODE`,
`OFFERS_MODE`, and the ceilings above. Optional: `USD_INR`, `GST_RATE`,
`SENTRY_DSN`.

## Before the switch

1. **Rotate every key.** The catalogue, PIM, offers, Central Inventory and
   Gemini keys were shared through a public Postman page and in chat.
2. `make vendor-estate` if the Search-proxy product service changed since
   `vendor/estate/SOURCE`; commit the result.
3. Build with `deploy.sh` (immutable tags; `docs/ROLLBACK.md`).
4. Wait for `/readyz` to say `ready` and `warm`.
5. `python -m src.tools.guided_scenarios https://<the-live-host>` - 50 of 50.
6. Speak to it from an Android phone in Chrome and an iPhone in Safari,
   over HTTPS, in Tamil and in English.
7. Open Voice Call once and watch the meter: it should stop at
   `MAX_SESSION_INR`.

## Roll back when

A paid call appears in a Guided journey; the day's ceiling is reached
before noon on normal traffic; price or stock on a card disagrees with the
shop; the error rate passes 1%. `docs/ROLLBACK.md` has the commands.
