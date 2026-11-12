# The 100 rules, against this repository

Checked against *Full Stack Development Rules* (React + Vite + TypeScript +
MUI + Redux Toolkit), 2026-11-12.

That stack is not this one. This is a Python voice service with one page of
plain JavaScript in front of it, so roughly a third of the rules describe a
frontend that does not exist here. Those are marked **n/a** with the reason,
not quietly skipped — a rule that does not apply and a rule that was ignored
look identical in a summary, and only one of them is fine.

Where a rule is about an idea rather than a library, it is applied in this
language: "Prettier and ESLint" is `ruff`, "enable strict checks" is
`mypy --strict`, "keep interfaces in .types.ts" is a dataclass or a Protocol
in the module that owns it.

| | |
|---|---|
| applied | 58 |
| not applicable to this stack | 33 |
| **open gaps** | **9** |

The nine gaps are listed at the bottom with what each would take. None is
hidden in the table.

---

## 01 Plan and create the project

| # | Rule | Where |
|---|---|---|
| 1 | Requirements | `../VOICE-COMMERCE-PLAN.md`, `../PHASE0-REPORT.md` |
| 2 | API plan | `src/agent/tools/declarations.py` is the contract the model codes against; `docs/` holds the rest |
| 3 | React, Vite, TypeScript | **n/a** — one page, no build step. A toolchain for one page is more build than product |
| 4 | Separate frontend and backend; app code in `src` | ✅ `src/` is the whole app; the page is `src/web/static/` |
| 5 | Env example, Docker and build settings at the root | ✅ `.env.example`, `Dockerfile`, `.dockerignore`, `Makefile`, `pytest.ini`, `mypy.ini` |
| 6 | Stable packages, no beta, commit the lockfile | ✅ every version pinned exactly in `requirements.txt`. **Gap 1**: pins are not hashed |
| 7 | Strict type checks, avoid `any` | ✅ `mypy.ini` sets `strict = True`; untyped third parties are listed one by one |
| 8 | Prettier and ESLint | ✅ `ruff check` and `ruff format`, enforced in CI and `make check` |

## 02 Organize the code

| # | Rule | Where |
|---|---|---|
| 9 | Modular monolith, a folder per feature | ✅ `agent/`, `voice/`, `chat/`, `web/`, `config/` |
| 10 | SRP — one clear job per file | ✅ the reason `runner.py` became `runner` + `shortlist` + `speech`, and `server.py` became `app` + `routes/` + `services/` |
| 11 | SOLID | ✅ every seam is a `Protocol`: `AudioSource`, `AudioSink`, `Backend`, `Socket`, `Streamer` |
| 12 | Dependency injection | ✅ `src/web/services/dependencies.py`; transports, sockets and HTTP clients are all passed in, which is what lets the suite run with no network |
| 13 | Loose coupling, no circular imports | ✅ `estate.build()` imports its backends inside the function for exactly this reason |
| 14 | KISS | ✅ |
| 15 | DRY | ✅ `cost_report.py` is one copy of a payload that had already been written twice; `tests/fakes.py` and `tests/payloads.py` are one copy of fixtures that three modules need |
| 16 | Patterns only when they solve something | ✅ |
| 17 | Files under 500 lines | ✅ largest handwritten file is 469 lines |
| 18 | Clear names | ✅ |
| 19 | Shared types in `.types.ts` | ✅ *adapted* — dataclasses and Protocols beside the code that owns them, which is where Python keeps them |

## 03 Build the backend

| # | Rule | Where |
|---|---|---|
| 20 | Routes map to controllers; no business logic in route files | ✅ `src/web/routes/` is 3 files, none over 60 lines, each one accept → delegate |
| 21 | Controllers read, call a service, respond | ✅ |
| 22 | Services hold the rules | ✅ `src/web/services/voice_call.py`, `chat_relay.py` |
| 23 | Repositories for data queries | ✅ *adapted* — no database; the data access layer is `catalogue.py` and `estate_backends.py`, separate from the services that call them |
| 24 | Validate incoming data | ✅ tool arguments are coerced and bounded in `runner.py`; the socket accepts binary as audio and JSON as control and nothing else |
| 25 | Consistent responses, errors and status codes | **Gap 2** — socket messages are consistent (`{"type": ...}`); HTTP still returns FastAPI's default error shape |
| 26 | Version breaking changes | ✅ `/api/v1/config`; the unversioned path is gone and a test asserts it 404s |
| 27 | Authentication | **n/a for Phase 1** — no accounts and no user data. Phase 2 identifies a caller by the telephony leg, not by a session cookie |
| 28 | Authorization | **n/a** — same |
| 29 | Rate limits | ✅ `src/web/ratelimit.py`, per client, on both sockets |

## 04 Set up the database

Rules 30–38: **n/a**. There is no database. Every fact the agent speaks is
fetched live from Poorvika's own services, on purpose — a cached price is a
price that can be wrong on a phone call.

## 05 Build the frontend screens

Rules 39, 41, 45, 46: **n/a** (MUI, theming, components, SPA routing).

| # | Rule | Where |
|---|---|---|
| 40 | Styles kept separate | ✅ one stylesheet block in `index.html`, no inline styles in JS |
| 42 | Responsive | ✅ the page works at phone width |
| 43 | Render business data from the API, never hardcode | ✅ every product, price and cost figure on screen came off the socket |
| 44 | Accessibility | ✅ `aria-label`, `aria-selected`, `aria-hidden` on the decorative SVGs. **Gap 3**: no keyboard path through the tabs and no contrast audit |
| 47 | Error boundaries | ✅ *adapted* — a failed call sends `{"type": "error"}` and the page shows it rather than freezing |
| 48 | Loading, empty, success and error states, no fake data | ✅ |

## 06 Manage state with Redux

Rules 49–58: **n/a**. No Redux and no React. The page holds one `state`
object, which is the KISS answer at this size.

Rule 57 is worth honouring anyway and is: no password, token or key is ever
sent to the browser, and `tests/test_web.py` fails if `/api/v1/config` starts
leaking one.

## 07 Connect the frontend and backend

Rules 60, 61, 62, 63, 64: **n/a** (Axios, thunks, feature-module exports,
interceptors).

| # | Rule | Where |
|---|---|---|
| 59 | Endpoint definitions separate from service functions | ✅ `routes/` and `services/` |
| 65 | One active request at a time | ✅ `Conversation._awaiting_reply` — one turn in flight; the bug it fixed was eleven overlapping turns and not one completed reply |
| 66 | Deduplicate reads | ✅ an identical search is answered from the previous result instead of going upstream |
| 67 | Cancel obsolete work | ✅ audio for a turn the caller talked over is dropped by turn id, however late it arrives |
| 68 | Release slots on every outcome | ✅ the latch is cleared in `TurnComplete` and on interruption |
| 69 | Useful errors, bounded retries | ✅ timeouts on every upstream call; "could not check" is reported as itself and never as "none" |

## 08 Improve performance

Rules 72–80: **n/a** (React rendering, memoisation, virtualisation).

| # | Rule | Where |
|---|---|---|
| 70 | CPU target, verified by profiling | **Gap 4** — latency is measured per turn, CPU is not |
| 71 | No debug console calls | ✅ zero `console.*` in the shipped page |
| 81 | Development checks on | ✅ `--reload` in dev, strict types, `--strict-markers` in pytest |
| 82 | Clean up timers and listeners | ✅ `cancelAnimationFrame` on stop; both asyncio pumps cancel each other |
| 83 | Caches expire and are cleared | ✅ every cache is per call and dies with the `ToolSession` |
| 84 | No localStorage or sessionStorage for app data | ✅ neither appears anywhere in the page |
| 85 | Remove unused code and packages | ✅ this pass removed `pipecat-ai` (declared, never imported), two unreferenced logos and a dead property; `httpx` was being used without being declared and now is |

## 09 Secure and monitor

| # | Rule | Where |
|---|---|---|
| 86 | One config module, safe `.env.example` | ✅ `src/config/` is the only place that reads `os.environ`; `.env.example` lists every name and no value |
| 87 | Secrets stay on the backend | ✅ the browser gets three numbers and a greeting, and a test enforces it |
| 88 | HTTPS | **Gap 5** — terminated by whatever fronts the container, which Phase 2 has not chosen |
| 89 | Safe HTML, restricted CORS, CSRF, validated uploads | ✅ CORS is an explicit origin list, never `*`; CSP, `nosniff`, `DENY` framing and a referrer policy on every response; no uploads exist |
| 90 | Capture errors after removing sensitive information | **Gap 6** — exceptions are logged with stack traces, but nothing ships them anywhere |
| 91 | Request ids in logs; never log secrets | ✅ `src/web/middleware.py`; method, path, status and duration only — never a header or a query string |
| 92 | Audit trail | ✅ `CallTrace` records every decision with a timestamp, and deliberately no audio and no speech |
| 93 | Watch errors, latency and resource use; health checks and alerts | ✅ `/healthz` and `/readyz`, plus per-turn latency on screen. **Gap 7**: no alerting |

## 10 Test, review and release

| # | Rule | Where |
|---|---|---|
| 94 | Unit tests on calculations, validation and business rules | ✅ 191 tests; the cost model is checked against a real metered bill |
| 95 | Integration and E2E, including failures | ✅ `TestClient` through the real app; the outage paths are tested harder than the happy ones |
| 96 | `.gitignore` at the root, excluding secrets and output | ✅ |
| 97 | Code review before merge | **process, not repository** — the GitHub workflow gates pull requests |
| 98 | `.github/workflows/*.yml` for formatting, types, tests and builds | ✅ `.github/workflows/ci.yml`, all four, plus a container build that must answer its own health check |
| 99 | Root Dockerfile and `.dockerignore`, multistage, runtime secrets | ✅ two stages, non-root user, no secret in any layer — `.env` is in `.dockerignore` as well as `.gitignore` |
| 100 | Test the production build, monitor the release, keep a rollback plan | ✅ CI builds the image and probes it. **Gap 8**: no rollback plan. **Gap 9**: the deploy target is not chosen |

---

## The nine open gaps

| | Gap | What it would take |
|---|---|---|
| 1 | Pins are not hashed | `pip-compile --generate-hashes`, and a lockfile to review |
| 2 | HTTP errors use FastAPI's default shape | one exception handler returning the same `{type, message}` the sockets use |
| 3 | No keyboard path or contrast audit | a tab order through the two tabs, and a contrast check against WCAG AA |
| 4 | CPU is not profiled | a profiler run under a real call, against a stated target |
| 5 | HTTPS is not terminated here | a Phase 2 decision; it depends on the telephony provider |
| 6 | Errors are logged, never shipped | Sentry or equivalent, with the request id attached and the payload stripped |
| 7 | No alerting | thresholds on latency, error rate and rupees a minute, wherever this ends up hosted |
| 8 | No rollback plan | write it down: previous image tag, how to switch, who decides |
| 9 | No deploy target | `deploy.sh` builds and stops, on purpose — Phase 2 |

Gaps 5, 8 and 9 are the same decision wearing three hats: where this runs.
Nothing else is blocked on it.
