# poorvika-voice

Inbound telephone voice agent for Poorvika. Tamil, English and Tanglish, over a
real phone call, under ₹2 per connected minute all-in.

Phase 1 of six. See `../PHASE0-REPORT.md` for the architecture decision and
`../COST-MODEL.md` for the measurements everything here rests on.

## Run it

```bash
make install            # venv + dependencies
cp .env.example .env    # then put the real GEMINI_API_KEY in it
make test
```

`make` on its own lists everything:

| | |
|---|---|
| `make ui` | **browser UI at http://localhost:8080** — costs money |
| `make talk` | same thing in the terminal — costs money |
| `make test` | the fast tests: no API, no devices, no money |
| `make audio` | device tests — needs a microphone and speaker |
| `make live` | real Gemini Live calls — spends a few cents |
| `make check` | everything CI runs, in the same order |
| `make lock` | recompile the lockfiles after editing a `.in` |
| `make fmt` | reformat in place |
| `make mic` | switch capture to the built-in microphone array |

`make ui` is the one to use for the Phase 1 measurement run: it shows which side
is speaking, what the agent said, and per-turn latency and tokens as they
happen, with a running p50/p95 against the targets.

`make test` does not call the API and does not spend money. Anything that does
is marked `live` and excluded by default, so neither CI nor a careless `make
test` can run up a bill.

## What this branch is not

There is no telephony. `src/voice/local_audio.py` drives a laptop microphone and
speaker through `arecord` and `aplay`, which is throwaway scaffolding behind the
transport seam — Phase 2 replaces both classes and nothing above
`src/voice/transport.py` changes.

**Phase 1 is not complete.** `src.tools.talk` runs and reports p50/p95, but
nobody has yet sat down and done the 10 Tamil + 10 Tanglish turns that the phase
completes on. Until that run exists, the latency numbers here are single samples
— enough to show the target is reachable and to catch a regression, not a
measurement.

## Folder structure

```
src/config/      devconfig.py / prodconfig.py, the per-environment split.
                 Nothing outside this package reads os.environ.
src/agent/       who the agent is and what it may claim.
  persona.py       the standing instruction, charged on every turn
  tools/           the catalogue, the estate, and the shortlist
src/voice/       the voice service: the Live adapter, VAD, barge-in,
                 the cost meter and the session ceilings.
src/chat/        the same agent, delivered to a reader.
src/web/         the browser surface, in three layers:
  app.py           the factory, and nothing else
  middleware.py    request id, security headers, no caching
  routes/          URL to handler. Thin on purpose.
  services/        what the handlers actually do.
  static/          one page, two scripts, no build step.
src/tools/       offline scripts: measurement, verification. Not shipped.
tests/           committed, unlike search-proxy's.
docs/            COST.md, PIPELINE.md, RULES.md.
```

A route holds no rules and a service holds no URLs. When that stops being
true, the thing in the wrong place is the one to move.

## Root files

| | |
|---|---|
| `.env.example` | every setting there is, with no value |
| `requirements.in` / `.txt` | intent, and the hashed lockfile compiled from it |
| `Dockerfile` / `.dockerignore` | multistage, non-root, no secret in any layer |
| `.github/workflows/ci.yml` | format, types, tests, secrets, and a container that must answer its own health check |
| `.gitlab-ci.yml` | the same checks, plus the deploy stage |
| `mypy.ini` | `strict = True` |
| `pytest.ini` | paid and device tests excluded by default |
| `Makefile` | the things we actually run |

## Operating it

| | |
|---|---|
| `GET /healthz` | is the process alive? Contacts nothing downstream |
| `GET /readyz` | is it configured? Names the missing capability, never its value |
| `x-request-id` | on every response, and on every log line for that request |

`docs/RULES.md` checks this repository against the full-stack rules one by
one, including the six gaps that are still open.


## Status

| | |
|---|---|
| Config, per-environment split | ✅ |
| Cost meter, priced per charge line | ✅ validated against a real metered bill |
| CI: format, strict types, tests, secret scan | ✅ GitHub and GitLab, same four |
| Routes / services split, versioned API | ✅ `/api/v1`, no rules in a route file |
| Health, readiness, request ids, rate limits | ✅ 15 tests |
| Container image | ✅ multistage, non-root; built and probed in CI |
| Gemini Live adapter | ✅ 9 offline tests + 3 against the real API |
| Thinking off, verified on the wire | ✅ `thoughtsTokenCount == 0` |
| Transport seam (`AudioSource` / `AudioSink`) | ✅ what Phase 2 swaps |
| Local mic in / speaker out | ✅ ALSA, no native dependency |
| Barge-in flush | ✅ measured under 300 ms on real hardware |
| Energy VAD | ✅ 60 ms to trigger, holds through a 400 ms pause |
| `python -m src.tools.talk` | ✅ runnable; prints p50/p95 and ₹/min |
| Browser UI (`make ui`) | ✅ transcript, live p50/p95, per-turn tokens |
| Transport seam carries a 2nd implementation | ✅ browser as well as ALSA |
| Chat tab on `gemini-3.5-flash-lite` | ✅ streaming, ~₹0.016/turn measured |
| One persona, both surfaces | ✅ `src/agent/persona.py` |
| Grounding holds — no invented prices | ✅ asked for prices, it offered to check |
| p50/p95 over 10 Tamil + 10 Tanglish turns | ⬜ **the Phase 1 completion criterion** |
| Telephony | ⬜ Phase 2 |

## What was reused, and what was deliberately not

Reused from `search-proxy`: the three-branch model (`development` → `stage` →
`main`), `deploy.sh` with its environment whitelist and early secret check, exact
dependency pinning, the per-environment config split, pytest with
`--strict-markers` and a `live` marker excluded by default, and the habit of
explaining *why* in a comment where a reader would otherwise ask.

**Not reused: the Cloudflare Workers runtime.** A voice session holds a socket
open for minutes and needs to drain cleanly on deploy. Workers do not express
that. The catalogue stays on the existing Worker and this service calls it as
a tool.
Reasoning in `../PHASE0-REPORT.md` §3.

**Three search-proxy habits deliberately corrected.** Its CI runs no checks
before deploying — ours runs formatting, strict types, tests and a secret scan
first, in both pipelines. It gitignores `tests/`, leaving them unversioned —
ours are committed. And it has no container story at all — ours is a root
`Dockerfile` that CI builds and starts on every change.

## Known limitations

1. **Over the cost target.** Poorvika wants ₹2.00 a connected minute; a real
   call runs at about ₹5.28. `docs/COST.md` says where every paisa goes and
   `docs/PIPELINE.md` prices the three ways out, each measured rather than
   assumed. The session ceiling that actually ends a call sits at ₹8, well
   above the target, because hanging up on a customer mid-sentence does not
   refund the rupee.
2. **Python 3.10.12.** Supported, and close enough to the end of that to be a
   cost of doing business rather than a preference. It should move to 3.11+
   before Phase 4.
3. **The deploy target is not configured.** `deploy.sh` builds the image and
   stops, because the host depends on which telephony provider terminates
   where. Phase 2.
4. **Stock is unavailable in production.** Poorvika has not issued a Central
   Inventory host and a token for the mode it is sent with, so the agent
   reports that stock could not be checked — which is the truth, and is a
   different sentence from "there is none".
5. **Six rules are open**, listed with what each would take at the bottom of
   `docs/RULES.md`.

## Security

- The key is read from a gitignored `.env`, never committed, never logged, never
  sent to the browser. `.env.example` carries placeholders only.
- CI secrets come from GitLab CI variables, scoped per environment, the same
  pattern `search-proxy` uses.
- `detect-secrets` runs in CI against a committed baseline.
- Call recording is **off**. The reference implementation records by default
  behind a consent click; that is a policy decision for Poorvika to make
  explicitly, not a default to inherit.
