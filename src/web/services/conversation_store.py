"""Conversation, replay and spend, shared by every worker that answers chat.

One process uses the memory backend. Several processes use Redis and the
same rules: a reservation is atomic across the conversation, the IST day
and the client, and repeating a reservation id does nothing the second time.

Amounts are integer paise, GST included. If the store cannot be reached,
paid work stops and history still redraws from whatever the browser kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from src.config import Config

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 24 * 60 * 60
HOLD_SECONDS = 120


def inr_to_paise(inr: float) -> int:
    return int(round(max(0.0, inr) * 100))


def paise_to_inr(paise: int) -> float:
    return round(paise / 100, 2)


def ist_day(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(now if now is not None else time.time(), IST)
    return moment.date().isoformat()


def mint_token(secret: str, conv_id: str, host_origin: str, exp: int) -> str:
    payload = json.dumps(
        {"v": 1, "c": conv_id, "o": host_origin, "e": exp},
        separators=(",", ":"),
    ).encode()
    body = _b64(payload)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"ct_v1.{body}.{sig}"


def read_token(secret: str, token: str) -> dict[str, Any] | None:
    try:
        version, body, sig = token.split(".")
    except ValueError:
        return None
    if version != "ct_v1" or not secret:
        return None
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_unb64(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or int(data.get("e") or 0) < int(time.time()):
        return None
    return data


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(body: str) -> bytes:
    import base64

    pad = "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(body + pad)


@dataclass
class Caps:
    conversation_paise: int
    client_paise: int
    day_paise: int


@dataclass
class ReserveResult:
    ok: bool
    reason: str = ""
    overrun: bool = False


@dataclass
class Claim:
    status: str
    text_hash: str = ""
    final_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Conversation:
    id: str
    host_origin: str
    host_session: str
    created: float
    updated: float
    absolute_exp: float
    pincode: str = ""
    product: dict[str, Any] | None = None
    context_label: str = "this page"
    slots: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scope_key(self) -> str:
        if self.host_session:
            return f"host:{self.host_session}"
        return f"conv:{self.id}"


@dataclass
class _Hold:
    rid: str
    amount: int
    keys: tuple[str, str, str]
    ok: bool
    reason: str
    created: float
    settled: bool = False
    released: bool = False
    actual: int = 0


class MemoryStore:
    """The single-process backend. One lock, so reserve cannot interleave."""

    def __init__(self, caps: Caps, secret: str) -> None:
        self._caps = caps
        self.secret = secret or uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._convs: dict[str, Conversation] = {}
        self._messages: dict[tuple[str, str], dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._holds: dict[str, _Hold] = {}
        self._spent: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._rate: dict[str, deque[float]] = {}
        self.overruns = 0

    def client_key(self, ip: str) -> str:
        digest = hashlib.sha256(f"{ip}|{self.secret}".encode()).hexdigest()[:32]
        return digest

    async def create(
        self,
        *,
        host_origin: str,
        host_session: str,
        pincode: str,
        product: dict[str, Any] | None,
    ) -> tuple[Conversation, str]:
        now = time.time()
        conv = Conversation(
            id=uuid.uuid4().hex,
            host_origin=host_origin,
            host_session=host_session[:64],
            created=now,
            updated=now,
            absolute_exp=now + ABSOLUTE_SECONDS,
            pincode=pincode,
            product=product,
        )
        async with self._lock:
            self._convs[conv.id] = conv
        return conv, self._token(conv)

    async def resume(
        self, token: str, host_origin: str
    ) -> tuple[Conversation, str] | None:
        data = read_token(self.secret, token)
        if data is None or data.get("o") != host_origin:
            return None
        async with self._lock:
            conv = self._convs.get(str(data.get("c")))
            if conv is None or conv.host_origin != host_origin:
                return None
            now = time.time()
            if now > conv.absolute_exp or now - conv.updated > IDLE_SECONDS:
                self._convs.pop(conv.id, None)
                return None
            conv.updated = now
        return conv, self._token(conv)

    async def save(self, conv: Conversation) -> None:
        async with self._lock:
            conv.updated = time.time()
            self._convs[conv.id] = conv

    async def claim_message(self, conv_id: str, message_id: str, text: str) -> Claim:
        digest = hashlib.sha256(text.encode()).hexdigest()
        async with self._lock:
            key = (conv_id, message_id)
            existing = self._messages.get(key)
            if existing is None:
                self._messages[key] = {
                    "status": "accepted",
                    "hash": digest,
                    "charge": 0,
                }
                return Claim("accepted", digest)
            if existing["hash"] != digest:
                return Claim("reused", existing["hash"])
            status = existing["status"]
            events = [
                event
                for event in self._events.get(conv_id, [])
                if event.get("message_id") == message_id
                and event.get("type") in {"final", "done"}
            ]
            if status == "done":
                return Claim("done", digest, events)
            return Claim("in_progress", digest, events)

    async def finish_message(
        self, conv_id: str, message_id: str, charge_paise: int
    ) -> None:
        async with self._lock:
            row = self._messages.get((conv_id, message_id))
            if row is not None:
                row["status"] = "done"
                row["charge"] = charge_paise

    async def fail_message(self, conv_id: str, message_id: str) -> None:
        async with self._lock:
            row = self._messages.get((conv_id, message_id))
            if row is not None and row["status"] != "done":
                row["status"] = "failed"

    async def append_event(self, conv_id: str, body: dict[str, Any]) -> int:
        async with self._lock:
            events = self._events.setdefault(conv_id, [])
            seq = len(events) + 1
            stored = dict(body)
            stored["seq"] = seq
            events.append(stored)
            if len(events) > 200:
                del events[:-200]
            return seq

    async def events_after(self, conv_id: str, seq: int) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                event
                for event in self._events.get(conv_id, [])
                if int(event["seq"]) > seq
            ]

    async def reserve(
        self, conv: Conversation, client_key: str, rid: str, amount: int
    ) -> ReserveResult:
        amount = max(0, int(amount))
        async with self._lock:
            self._reap_locked()
            existing = self._holds.get(rid)
            if existing is not None:
                return ReserveResult(existing.ok, existing.reason)
            keys = (
                conv.scope_key,
                f"day:{ist_day()}",
                f"client:{client_key}:{ist_day()}",
            )
            caps = (
                self._caps.conversation_paise,
                self._caps.day_paise,
                self._caps.client_paise,
            )
            for key, cap in zip(keys, caps):
                if self._spent.get(key, 0) + self._reserved.get(key, 0) + amount > cap:
                    scope = key.split(":", 1)[0]
                    self._holds[rid] = _Hold(
                        rid, amount, keys, False, scope, time.time()
                    )
                    return ReserveResult(False, scope)
            for key in keys:
                self._reserved[key] = self._reserved.get(key, 0) + amount
            self._holds[rid] = _Hold(rid, amount, keys, True, "ok", time.time())
            return ReserveResult(True, "ok")

    async def settle(self, rid: str, actual: int) -> ReserveResult:
        actual = max(0, int(actual))
        async with self._lock:
            hold = self._holds.get(rid)
            if hold is None or not hold.ok or hold.settled or hold.released:
                return ReserveResult(False, "missing")
            overrun = actual > hold.amount
            for key in hold.keys:
                self._reserved[key] = max(0, self._reserved.get(key, 0) - hold.amount)
                self._spent[key] = self._spent.get(key, 0) + actual
            hold.settled = True
            hold.actual = actual
            if overrun:
                self.overruns += 1
            return ReserveResult(True, "ok", overrun)

    async def release(self, rid: str) -> None:
        async with self._lock:
            hold = self._holds.get(rid)
            if hold is None or not hold.ok or hold.settled or hold.released:
                return
            for key in hold.keys:
                self._reserved[key] = max(0, self._reserved.get(key, 0) - hold.amount)
            hold.released = True

    async def spent_paise(self, conv: Conversation) -> int:
        async with self._lock:
            return self._spent.get(conv.scope_key, 0)

    async def allow(
        self, scope: str, client: str, limit: int, now: float | None = None
    ) -> bool:
        if limit <= 0:
            return True
        moment = time.monotonic() if now is None else now
        async with self._lock:
            key = f"{scope}:{client}"
            starts = self._rate.setdefault(key, deque())
            cutoff = moment - 60.0
            while starts and starts[0] < cutoff:
                starts.popleft()
            if len(starts) >= limit:
                return False
            starts.append(moment)
            return True

    def _token(self, conv: Conversation) -> str:
        return mint_token(
            self.secret, conv.id, conv.host_origin, int(conv.absolute_exp)
        )

    def _reap_locked(self) -> None:
        now = time.time()
        for hold in list(self._holds.values()):
            if (
                hold.ok
                and not hold.settled
                and not hold.released
                and now - hold.created > HOLD_SECONDS
            ):
                for key in hold.keys:
                    self._reserved[key] = max(
                        0, self._reserved.get(key, 0) - hold.amount
                    )
                hold.released = True


def _encode(parts: tuple[str, ...]) -> bytes:
    out = f"*{len(parts)}\r\n".encode()
    for part in parts:
        blob = part.encode()
        out += f"${len(blob)}\r\n".encode() + blob + b"\r\n"
    return out


async def _read_resp(reader: asyncio.StreamReader) -> Any:
    kind = await reader.readexactly(1)
    if kind == b"+":
        return (await reader.readline()).rstrip(b"\r\n").decode()
    if kind == b"-":
        raise RuntimeError((await reader.readline()).decode("utf-8", "replace"))
    if kind == b":":
        return int((await reader.readline()).strip())
    if kind == b"$":
        size = int((await reader.readline()).strip())
        if size < 0:
            return None
        data = await reader.readexactly(size)
        await reader.readexactly(2)
        return data.decode()
    if kind == b"*":
        count = int((await reader.readline()).strip())
        if count < 0:
            return None
        return [await _read_resp(reader) for _ in range(count)]
    raise RuntimeError(f"unexpected redis reply {kind!r}")


# One EVAL, so two workers cannot both pass a cap. The hold records the three
# scope keys and the time it was taken, which is what settle, release and the
# reaper need. A repeated rid returns the first result and moves no counters.
_RESERVE_LUA = """
local existing = redis.call('GET', KEYS[4])
if existing then return existing end
local amount = tonumber(ARGV[1])
local function room(key, cap)
  local spent = tonumber(redis.call('GET', 'spent:'..key) or '0')
  local held = tonumber(redis.call('GET', 'rsv:'..key) or '0')
  return (spent + held + amount) <= tonumber(cap)
end
if not room(KEYS[1], ARGV[2]) then return '0|conversation' end
if not room(KEYS[2], ARGV[3]) then return '0|day' end
if not room(KEYS[3], ARGV[4]) then return '0|client' end
redis.call('INCRBY', 'rsv:'..KEYS[1], amount)
redis.call('INCRBY', 'rsv:'..KEYS[2], amount)
redis.call('INCRBY', 'rsv:'..KEYS[3], amount)
local saved = '1|ok|'..ARGV[1]..'|'..KEYS[1]..'|'..KEYS[2]..'|'..KEYS[3]..'|'..ARGV[5]
redis.call('SET', KEYS[4], saved)
redis.call('SADD', 'holdids', KEYS[4])
return '1|ok'
"""

_SETTLE_LUA = """
local saved = redis.call('GET', KEYS[1])
if not saved then return '0|missing' end
local ok, state, amount, k1, k2, k3 = string.match(
  saved, '^([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)')
if ok ~= '1' or state ~= 'ok' then return saved end
local actual = tonumber(ARGV[1])
local reserved = tonumber(amount)
for _, key in ipairs({k1, k2, k3}) do
  redis.call('DECRBY', 'rsv:'..key, reserved)
  redis.call('INCRBY', 'spent:'..key, actual)
end
local flag = 'ok'
if actual > reserved then flag = 'over' end
redis.call('SET', KEYS[1], '1|settled|'..ARGV[1]..'|'..k1..'|'..k2..'|'..k3..'|'..flag)
redis.call('SREM', 'holdids', KEYS[1])
return '1|'..flag
"""

_RELEASE_LUA = """
local saved = redis.call('GET', KEYS[1])
if not saved then return '0|missing' end
local ok, state, amount, k1, k2, k3 = string.match(
  saved, '^([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)')
if ok ~= '1' or state ~= 'ok' then return saved end
local reserved = tonumber(amount)
for _, key in ipairs({k1, k2, k3}) do
  redis.call('DECRBY', 'rsv:'..key, reserved)
end
redis.call('SET', KEYS[1], '1|released|'..amount..'|'..k1..'|'..k2..'|'..k3)
redis.call('SREM', 'holdids', KEYS[1])
return '1|released'
"""

_REAP_LUA = """
local now = tonumber(ARGV[1])
local stale = tonumber(ARGV[2])
local n = 0
for _, id in ipairs(redis.call('SMEMBERS', 'holdids')) do
  local saved = redis.call('GET', id)
  if not saved then
    redis.call('SREM', 'holdids', id)
  else
    local ok, state, amount, k1, k2, k3, ts = string.match(
      saved, '^([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)')
    if ok == '1' and state == 'ok' and ts and (now - tonumber(ts)) > stale then
      local reserved = tonumber(amount)
      for _, key in ipairs({k1, k2, k3}) do
        redis.call('DECRBY', 'rsv:'..key, reserved)
      end
      redis.call('SET', id, '1|released|'..amount..'|'..k1..'|'..k2..'|'..k3)
      redis.call('SREM', 'holdids', id)
      n = n + 1
    end
  end
end
return n
"""


class RedisStore(MemoryStore):
    """Same records as the memory store, with money moved in one Lua call.

    A connection failure refuses the reservation. The in-memory dicts on the
    base class are not consulted: every read and write goes to Redis, so a
    second process sees the same cap.
    """

    def __init__(self, url: str, caps: Caps, secret: str) -> None:
        super().__init__(caps, secret)
        parsed = urlparse(url)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 6379
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._io = asyncio.Lock()

    async def _call(self, *parts: str) -> Any:
        async with self._io:
            if self._writer is None:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port), 0.3
                )
            assert self._writer is not None and self._reader is not None
            self._writer.write(_encode(parts))
            await self._writer.drain()
            return await _read_resp(self._reader)

    async def _safe(self, *parts: str) -> Any:
        try:
            return await self._call(*parts)
        except Exception:
            self._writer = None
            self._reader = None
            log.warning("conversation store unavailable; paid chat stops")
            return None

    async def reserve(
        self, conv: Conversation, client_key: str, rid: str, amount: int
    ) -> ReserveResult:
        keys = (
            conv.scope_key,
            f"day:{ist_day()}",
            f"client:{client_key}:{ist_day()}",
            f"hold:{rid}",
        )
        raw = await self._safe(
            "EVAL",
            _RESERVE_LUA,
            "4",
            *keys,
            str(max(0, amount)),
            str(self._caps.conversation_paise),
            str(self._caps.day_paise),
            str(self._caps.client_paise),
            str(int(time.time())),
        )
        if not isinstance(raw, str):
            return ReserveResult(False, "store_unavailable")
        if raw == "1|ok" or raw.startswith("1|ok|"):
            return ReserveResult(True, "ok")
        _ok, _, reason = (raw + "||").split("|", 2)
        return ReserveResult(False, reason or "conversation")

    async def settle(self, rid: str, actual: int) -> ReserveResult:
        raw = await self._safe(
            "EVAL", _SETTLE_LUA, "1", f"hold:{rid}", str(max(0, actual))
        )
        if not isinstance(raw, str):
            return ReserveResult(False, "store_unavailable")
        overrun = raw == "1|over"
        if overrun:
            self.overruns += 1
        return ReserveResult(
            raw.startswith("1"), "ok" if raw.startswith("1") else raw, overrun
        )

    async def release(self, rid: str) -> None:
        await self._safe("EVAL", _RELEASE_LUA, "1", f"hold:{rid}")

    async def reap(
        self, *, now: float | None = None, stale: float = HOLD_SECONDS
    ) -> int:
        moment = time.time() if now is None else now
        raw = await self._safe(
            "EVAL", _REAP_LUA, "0", str(int(moment)), str(int(stale))
        )
        return int(raw) if isinstance(raw, int) else 0

    async def spent_key(self, key: str) -> int:
        raw = await self._safe("GET", f"spent:{key}")
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
        return 0

    async def create(
        self,
        *,
        host_origin: str,
        host_session: str,
        pincode: str,
        product: dict[str, Any] | None,
    ) -> tuple[Conversation, str]:
        now = time.time()
        conv = Conversation(
            id=uuid.uuid4().hex,
            host_origin=host_origin,
            host_session=host_session[:64],
            created=now,
            updated=now,
            absolute_exp=now + ABSOLUTE_SECONDS,
            pincode=pincode,
            product=product,
        )
        await self._write_conv(conv)
        return conv, self._token(conv)

    async def resume(
        self, token: str, host_origin: str
    ) -> tuple[Conversation, str] | None:
        data = read_token(self.secret, token)
        if data is None or data.get("o") != host_origin:
            return None
        conv = await self._read_conv(str(data.get("c")))
        if conv is None or conv.host_origin != host_origin:
            return None
        now = time.time()
        if now > conv.absolute_exp or now - conv.updated > IDLE_SECONDS:
            return None
        conv.updated = now
        await self._write_conv(conv)
        return conv, self._token(conv)

    async def save(self, conv: Conversation) -> None:
        conv.updated = time.time()
        await self._write_conv(conv)

    async def _write_conv(self, conv: Conversation) -> None:
        body = json.dumps(
            {
                "id": conv.id,
                "host_origin": conv.host_origin,
                "host_session": conv.host_session,
                "created": conv.created,
                "updated": conv.updated,
                "absolute_exp": conv.absolute_exp,
                "pincode": conv.pincode,
                "product": conv.product,
                "context_label": conv.context_label,
                "slots": conv.slots,
                "history": conv.history,
            }
        )
        await self._safe("SET", f"conv:{conv.id}", body, "EX", str(IDLE_SECONDS))

    async def _read_conv(self, conv_id: str) -> Conversation | None:
        raw = await self._safe("GET", f"conv:{conv_id}")
        if not isinstance(raw, str):
            return None
        data = json.loads(raw)
        return Conversation(
            id=data["id"],
            host_origin=data["host_origin"],
            host_session=data["host_session"],
            created=data["created"],
            updated=data["updated"],
            absolute_exp=data["absolute_exp"],
            pincode=data.get("pincode") or "",
            product=data.get("product"),
            context_label=data.get("context_label") or "this page",
            slots=data.get("slots") or {},
            history=data.get("history") or [],
        )

    async def claim_message(self, conv_id: str, message_id: str, text: str) -> Claim:
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = f"msg:{conv_id}:{message_id}"
        created = await self._safe(
            "SET",
            key,
            json.dumps({"status": "accepted", "hash": digest, "charge": 0}),
            "NX",
            "EX",
            "86400",
        )
        if created == "OK":
            return Claim("accepted", digest)
        raw = await self._safe("GET", key)
        if not isinstance(raw, str):
            return Claim("reused", "")
        existing = json.loads(raw)
        if existing.get("hash") != digest:
            return Claim("reused", str(existing.get("hash") or ""))
        events = await self.events_after(conv_id, 0)
        own = [
            event
            for event in events
            if event.get("message_id") == message_id
            and event.get("type") in {"final", "done"}
        ]
        status = "done" if existing.get("status") == "done" else "in_progress"
        return Claim(status, digest, own)

    async def finish_message(
        self, conv_id: str, message_id: str, charge_paise: int
    ) -> None:
        key = f"msg:{conv_id}:{message_id}"
        raw = await self._safe("GET", key)
        if not isinstance(raw, str):
            return
        existing = json.loads(raw)
        existing["status"] = "done"
        existing["charge"] = charge_paise
        await self._safe("SET", key, json.dumps(existing), "EX", "86400")

    async def fail_message(self, conv_id: str, message_id: str) -> None:
        key = f"msg:{conv_id}:{message_id}"
        raw = await self._safe("GET", key)
        if not isinstance(raw, str):
            return
        existing = json.loads(raw)
        if existing.get("status") != "done":
            existing["status"] = "failed"
            await self._safe("SET", key, json.dumps(existing), "EX", "86400")

    async def append_event(self, conv_id: str, body: dict[str, Any]) -> int:
        seq = await self._safe("INCR", f"seq:{conv_id}")
        if not isinstance(seq, int):
            return 0
        stored = dict(body)
        stored["seq"] = seq
        await self._safe("RPUSH", f"events:{conv_id}", json.dumps(stored))
        await self._safe("LTRIM", f"events:{conv_id}", "-200", "-1")
        return seq

    async def events_after(self, conv_id: str, seq: int) -> list[dict[str, Any]]:
        raw = await self._safe("LRANGE", f"events:{conv_id}", "0", "-1")
        if not isinstance(raw, list):
            return []
        events = []
        for item in raw:
            if isinstance(item, str):
                event = json.loads(item)
                if int(event.get("seq") or 0) > seq:
                    events.append(event)
        return events

    async def spent_paise(self, conv: Conversation) -> int:
        raw = await self._safe("GET", f"spent:{conv.scope_key}")
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
        return 0

    async def allow(
        self, scope: str, client: str, limit: int, now: float | None = None
    ) -> bool:
        if limit <= 0:
            return True
        moment = time.time() if now is None else now
        key = f"rl:{scope}:{client}"
        member = f"{moment}:{uuid.uuid4().hex[:6]}"
        added = await self._safe("ZADD", key, str(moment), member)
        if added is None:
            return False
        await self._safe("ZREMRANGEBYSCORE", key, "-inf", str(moment - 60))
        count = await self._safe("ZCARD", key)
        await self._safe("EXPIRE", key, "120")
        if not isinstance(count, int) or count > limit:
            await self._safe("ZREM", key, member)
            return False
        return True


def build_store(settings: Config) -> MemoryStore:
    caps = Caps(
        conversation_paise=inr_to_paise(settings.chat_session_cap_inr),
        client_paise=inr_to_paise(settings.chat_client_cap_inr),
        day_paise=inr_to_paise(settings.daily_spend_cap_inr),
    )
    secret = settings.conversation_token_secret
    if settings.redis_url:
        return RedisStore(settings.redis_url, caps, secret)
    return MemoryStore(caps, secret)
