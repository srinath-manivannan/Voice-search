# poorvika-voice

Inbound telephone voice agent for Poorvika. Tamil, English and Tanglish, over a
real phone call, under ₹2 per connected minute all-in.

Phase 1 of six. See `../PHASE0-REPORT.md` for the architecture decision and
`../COST-MODEL.md` for the measurements everything here rests on.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # then put the real GEMINI_API_KEY in it
.venv/bin/python -m pytest
```

Tests do not call the API and do not spend money. Anything that does is marked
`live` and excluded by default:

```bash
.venv/bin/python -m pytest -m live      # spends money, run deliberately
.venv/bin/python -m pytest -m audio     # needs a microphone and speaker
```

To actually talk to it:

```bash
.venv/bin/python -m src.tools.talk --seconds 120
```

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
src/voice/       the voice service. usage.py is the cost meter.
src/tools/       offline scripts: measurement, verification. Not shipped.
tests/           committed, unlike search-proxy's.
```

## Status

| | |
|---|---|
| Config, per-environment split | ✅ |
| Cost meter, both billing hypotheses | ✅ 10 tests |
| CI: lint, typecheck, test, secret scan | ✅ all four pass locally |
| Pipecat 0.0.108 on Python 3.10 | ✅ imports, `GeminiLiveLLMService` present |
| Gemini Live adapter | ✅ 9 offline tests + 3 against the real API |
| Thinking off, verified on the wire | ✅ `thoughtsTokenCount == 0` |
| Transport seam (`AudioSource` / `AudioSink`) | ✅ what Phase 2 swaps |
| Local mic in / speaker out | ✅ ALSA, no native dependency |
| Barge-in flush | ✅ measured under 300 ms on real hardware |
| Energy VAD | ✅ 60 ms to trigger, holds through a 400 ms pause |
| `python -m src.tools.talk` | ✅ runnable; prints p50/p95 and ₹/min |
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
that, and the Python Workers runtime will not carry Pipecat's dependencies. The
catalogue stays on the existing Worker and this service calls it as a tool.
Reasoning in `../PHASE0-REPORT.md` §3.

**Two search-proxy habits deliberately corrected.** Its CI runs no checks before
deploying — ours runs lint, typecheck, tests and a secret scan first. And it
gitignores `tests/`, leaving them unversioned — ours are committed.

## Known limitations

1. **Python 3.10.12.** `google-api-core` now warns that 3.10 is unsupported and
   will get no further updates. Moving to 3.11+ is a cost of doing business, not
   a preference, and it should happen before Phase 4.
2. **PyTorch is absent**, so `transformers` is tokenizer-only. Fine for the
   chosen architecture — the model does the work remotely — but Pipecat's
   smart-turn detection would need it.
3. **The deploy target is not configured.** `deploy.sh` builds the image and
   stops, because the host depends on which telephony provider terminates where.
   Phase 2.
4. **Whether the accumulated prompt is re-billed per turn is unresolved**, so
   `usage.py` reports both figures rather than inventing one. They differ by
   about 3.3×. See `../BILLING-VERIFICATION.md`.

## Security

- The key is read from a gitignored `.env`, never committed, never logged, never
  sent to the browser. `.env.example` carries placeholders only.
- CI secrets come from GitLab CI variables, scoped per environment, the same
  pattern `search-proxy` uses.
- `detect-secrets` runs in CI against a committed baseline.
- Call recording is **off**. The reference implementation records by default
  behind a consent click; that is a policy decision for Poorvika to make
  explicitly, not a default to inherit.
