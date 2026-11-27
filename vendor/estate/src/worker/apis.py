"""The retailer API estate — one switch, four services, one HTTP layer.

`API_ENV` picks the whole estate at once, stage or production. No caller
chooses per service, because the failure worth designing against is not an
outage, it is a *mixed* answer: production pricing printed beside stage
stock, both with the same confidence. A host that does not belong to the
selected environment is refused here rather than called.

Four services, four different authentication schemes, and they are not
interchangeable:

    search   access-token   + Origin: bot
    pim      access-token   + channel + locale
    offers   x-api-key      + channel + mode: mobile_web
    ci       authorization  + channel + mode: ai_bot

`mode` is not a label. Measured on 2026-11-04: the CI gateway verifies the
bearer token with a secret chosen BY `mode`, so the same token is accepted
with `mode: bot` and rejected with `mode: ai_bot` — and the rejection reads
`signature is invalid`, which looks like a bad token rather than a bad pair.
Mode is therefore configuration, not a constant, and travels with its token.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

import js
from pyodide.ffi import to_js

# Deployment bindings supply each service's full base URL.
ENVIRONMENTS = {"stage", "production"}
DEFAULT_ENV = "stage"

# Per-service: the header its credential goes in, the secret that holds it,
# the variable that overrides its host, and how long one call may take.
# Timeouts come from measurement, not taste — see docs/product-service.md.
SERVICES: dict[str, dict] = {
    "pim": {"header": "access-token", "secret": "PIM_ACCESS_TOKEN",
            "host_var": "PIM_HOST", "timeout_ms": 4000},
    "offers": {"header": "x-api-key", "secret": "OFFERS_API_KEY",
               "host_var": "OFFERS_HOST", "timeout_ms": 3000},
    "ci": {"header": "authorization", "secret": "CI_AUTH_TOKEN",
           "host_var": "CI_HOST", "timeout_ms": 2500},
    # No secret: this one is open, and saying so here is better than
    # inventing a credential nobody checks.
    "emi": {"header": None, "secret": None, "host_var": "EMI_HOST",
            "timeout_ms": 3000},
    "reviews": {"header": None, "secret": None, "host_var": "REVIEWS_HOST",
                "timeout_ms": 3000, "cross_env": True},
}

# Fixed headers a service always wants, and the variable that can change one.
# `mode` is here because it participates in authentication (see above).
MODES = {"offers": ("OFFERS_MODE", "mobile_web"), "ci": ("CI_MODE", "ai_bot")}

# How long an answer may be reused. Specifications move when a merchandiser
# edits them; stock moves while the shopper reads the page. The two cannot
# share a TTL, and every one of these is a starting assumption meant to be
# tuned from the hit-rate the service reports, not a measured optimum.
TTL_SECONDS = {"pim_product": 600, "pim_pricing": 120, "ci_stock": 30,
               # "Delivery by 1:41 PM Today" moves with the clock.
               "ci_delivery": 60,
               # Bought-together and related carry prices, so a price's TTL.
               "pim_lists": 120,
               "offers_labels": 300, "offers_detail": 300, "nbfc_finance": 600,
               # Keyed by price, so it only goes stale when the price does.
               "emi_plans": 300,
               # A review is written once and read for months.
               "reviews": 600}

CACHE_MAX = 256

# Status words. Six, because six things genuinely differ, and collapsing any
# two of them is how "we could not ask" becomes "there is none".
OK = "ok"                      # answered, with content
EMPTY = "empty"                # answered, and the answer is nothing
UNAVAILABLE = "unavailable"    # refused, or the upstream broke
TIMEOUT = "timeout"            # no answer inside the budget
INVALID = "invalid"            # answered something we cannot read
NOT_CONFIGURED = "not_configured"   # we were never given what to call
NOT_REQUESTED = "not_requested"     # nobody asked


def _js(value):
    return to_js(value, dict_converter=js.Object.fromEntries)


def _var(env, name: str, default: str = "") -> str:
    val = getattr(env, name, None)
    if val is None and name:
        val = os.environ.get(name)
    return (val or default).strip()


def api_env(env) -> str:
    name = _var(env, "API_ENV", DEFAULT_ENV).lower()
    return name if name in ENVIRONMENTS else DEFAULT_ENV


def host(env, service: str) -> tuple[str | None, str | None]:
    """(host, problem). A host from the wrong environment is not returned.

    The check is crude on purpose — a `stage-` prefix — but it catches the
    mistake that actually happens, which is a stage host left in a secret
    after a copy, or the documentation's own production-folder slip.
    """
    name = api_env(env)
    host_var = SERVICES[service]["host_var"]
    chosen = _var(env, host_var)
    if not chosen:
        return None, f"no {service} host for {name}: set {host_var} to its full base URL"
    staging_host = "stage-" in chosen or "staging" in chosen
    # A public, read-only service with no per-environment twin may be read
    # from either estate; nothing it says depends on which one is asking.
    if SERVICES.get(service, {}).get("cross_env"):
        return chosen.rstrip("/"), None
    if (name == "production") != (not staging_host):
        return None, (f"{service} host {chosen.split('//')[-1].split('/')[0]} "
                      f"does not belong to {name} — refusing to mix")
    return chosen.rstrip("/"), None


def configured(env, service: str) -> tuple[bool, str | None]:
    where, problem = host(env, service)
    if problem:
        return False, problem
    secret = SERVICES[service]["secret"]
    if secret and not _var(env, secret):
        return False, (f"{secret} is not set, so {service} cannot be called")
    return True, None


def describe(env) -> dict:
    """What this Worker would call, and what it cannot. No secrets, ever."""
    name = api_env(env)
    out = {"api_env": name, "services": {}}
    for service in SERVICES:
        ready, problem = configured(env, service)
        where, _ = host(env, service)
        out["services"][service] = {
            "ready": ready,
            "host": (where or "").split("//")[-1].split("/")[0] or None,
            **({"problem": problem} if problem else {}),
        }
    return out


class Deadline:
    """One budget for the whole request, shared by every call it makes.

    Without it each service gets its own timeout and a page that asked for
    five sections can legitimately take the sum of five timeouts. With it,
    a slow second call shortens the third rather than adding to it.
    """

    def __init__(self, total_ms: int):
        self.until = time.time() + total_ms / 1000.0

    def remaining_ms(self) -> int:
        return max(0, int((self.until - time.time()) * 1000))

    def budget(self, want_ms: int) -> int:
        return min(want_ms, self.remaining_ms())


_CACHE: dict[str, tuple[float, dict]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_STATS = {"hits": 0, "misses": 0, "joined": 0}


def cache_stats() -> dict:
    return dict(_STATS, entries=len(_CACHE))


def _remember(key: str, ttl: int, payload: dict) -> None:
    if ttl <= 0:
        return
    if len(_CACHE) >= CACHE_MAX:
        for stale in list(_CACHE)[: CACHE_MAX // 4]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (time.time() + ttl, payload)


def _recall(key: str) -> dict | None:
    found = _CACHE.get(key)
    if not found:
        return None
    expires, payload = found
    if expires <= time.time():
        _CACHE.pop(key, None)
        return None
    return payload


def _gist(text: str) -> str:
    """The first words of an error body. A gateway's HTML error page
    (nginx's "<html><head><title>400 Bad Request…") is cut down to its text,
    so the reason reads "400 Bad Request nginx" wherever it is shown."""
    if "<" in text[:200]:
        text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split())[:160]


async def _request(url: str, headers: dict, timeout_ms: int) -> dict:
    started = time.time()
    try:
        response = await js.fetch(url, _js({
            "method": "GET", "headers": headers,
            "signal": js.AbortSignal.timeout(timeout_ms)}))
        text = await response.text()
        code = int(response.status)
    except Exception as exc:                                   # noqa: BLE001
        word = f"{type(exc).__name__} {exc}".lower()
        timed_out = "abort" in word or "timeout" in word or "timed out" in word
        return {"status": TIMEOUT if timed_out else UNAVAILABLE,
                "code": None, "took_ms": int((time.time() - started) * 1000),
                "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    took = int((time.time() - started) * 1000)
    # The status line decides whether we were ANSWERED; the body decides what
    # the answer says. Keeping those apart matters in both directions here:
    #
    #   200 + {"message": "signature is invalid"}   answered, and refused
    #   404 + {"status": "success", "data": []}     answered, and empty
    #   503 + {"error": "upstream"}                 not answered — retry
    #
    # So a 5xx or a 4xx-other-than-404 is an upstream failure however well
    # formed its body is, and 404 is passed through for the adapter to read,
    # because the NBFC endpoint uses it to mean "no finance on this product".
    if code >= 500 or (code >= 400 and code != 404):
        return {"status": UNAVAILABLE, "code": code, "took_ms": took,
                "detail": f"HTTP {code}: {_gist(text)}"}
    try:
        payload = json.loads(text)
    except Exception:                                          # noqa: BLE001
        return {"status": INVALID, "code": code, "took_ms": took,
                "detail": f"not JSON: {_gist(text)}"}
    return {"status": OK, "code": code, "took_ms": took, "body": payload,
            "text_len": len(text)}


async def fetch(env, service: str, path: str, *, kind: str, channel: str,
                deadline: Deadline, locale: str = "en-IN",
                query: str = "", retry: bool = True) -> dict:
    """One call to one service, cached, deduplicated, inside the deadline."""
    ready, problem = configured(env, service)
    if not ready:
        return {"status": NOT_CONFIGURED, "detail": problem, "took_ms": 0}

    where, _ = host(env, service)
    url = f"{where}{path}{query}"
    headers = {"Accept": "application/json", "channel": channel.upper()}
    if SERVICES[service]["header"]:
        headers[SERVICES[service]["header"]] = _var(
            env, SERVICES[service]["secret"])
    if service == "pim":
        headers["locale"] = locale
    if service in MODES:
        var, default = MODES[service]
        headers["mode"] = _var(env, var, default)

    key = f"{api_env(env)}|{service}|{url}|{channel}|{locale}|{headers.get('mode','')}"
    if (hit := _recall(key)) is not None:
        _STATS["hits"] += 1
        return dict(hit, cached=True, took_ms=0)

    # Two sections of one page, or two products in a comparison, can want the
    # same call. The second waits on the first rather than making it again.
    if (running := _INFLIGHT.get(key)) is not None:
        _STATS["joined"] += 1
        try:
            return dict(await asyncio.shield(running), joined=True)
        except Exception:                                      # noqa: BLE001
            pass

    _STATS["misses"] += 1

    async def run() -> dict:
        budget = deadline.budget(SERVICES[service]["timeout_ms"])
        if budget < 150:
            return {"status": TIMEOUT, "took_ms": 0,
                    "detail": "the request budget was spent before this call"}
        got = await _request(url, headers, budget)
        # One retry, and only where it can help: a timeout or a 5xx, on a
        # GET, with enough budget left to finish. Retrying a 4xx or a
        # rejected token just doubles the load during an incident.
        if (retry and got["status"] in (TIMEOUT, UNAVAILABLE)
                and (got.get("code") is None or got["code"] >= 500)
                and deadline.remaining_ms() > SERVICES[service]["timeout_ms"]):
            again = await _request(url, headers,
                                   deadline.budget(SERVICES[service]["timeout_ms"]))
            again["retried"] = True
            if again["status"] == OK:
                got = again
            else:
                got = dict(got, retried=True)
        if got["status"] == OK:
            _remember(key, TTL_SECONDS.get(kind, 60), got)
        return got

    task = asyncio.ensure_future(run())
    _INFLIGHT[key] = task
    try:
        return await task
    finally:
        _INFLIGHT.pop(key, None)
