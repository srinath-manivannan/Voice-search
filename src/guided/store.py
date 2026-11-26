"""Guided session snapshots and exactly-once ranking admission, with no audio or transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict, fields
from typing import Any

from src.guided.engine import Answer
from src.guided.recommend import Pick
from src.guided.sessions import Session, Sessions, UnknownSession
from src.guided.shelf import Item

Reservation = Callable[[], Awaitable[bool]]
reserve_rank: ContextVar[Reservation | None] = ContextVar("reserve_rank", default=None)
_PRIVATE = {
    "id",
    "facts",
    "answers",
    "ranked",
    "shown",
    "pending",
    "branches",
    "missed",
}


def snapshot(s: Session) -> str:
    values = {f.name: getattr(s, f.name) for f in fields(s) if f.name not in _PRIVATE}
    return json.dumps(
        {
            **values,
            "answers": [asdict(a) for a in s.answers],
            "ranked": [asdict(p) for p in s.ranked],
            "shown": [p.ref for p in s.shown],
            "pending": s.pending,
            "branches": s.branches,
        },
        ensure_ascii=False,
    )


def restore(sid: str, encoded: str) -> Session:
    data = json.loads(encoded)
    data["answers"] = tuple(Answer(**a) for a in data["answers"])
    picks = []
    for row in data["ranked"]:
        product = row.pop("item")
        product["specs"] = tuple(map(tuple, product["specs"]))
        product["pim_fields"] = tuple(product.get("pim_fields", ()))
        picks.append(Pick(item=Item(**product), **row))
    by_ref = {p.ref: p for p in picks}
    data.update(
        ranked=tuple(picks),
        shown=tuple(by_ref[r] for r in data["shown"]),
        branches=tuple(data["branches"]),
        pending=tuple(map(tuple, data["pending"])),
    )
    return Session(id=sid, **data)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS guided_sessions (
 id text PRIMARY KEY, state jsonb NOT NULL, touched timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS guided_requests (
 session_id text REFERENCES guided_sessions ON DELETE CASCADE, id text, signature text NOT NULL,
 response jsonb NOT NULL, PRIMARY KEY(session_id,id)
);
CREATE TABLE IF NOT EXISTS guided_rank_spend (
 session_id text PRIMARY KEY, reserved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS guided_decisions (
 session_hash text, created_at timestamptz NOT NULL DEFAULT now(), decision jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS guided_sessions_touched ON guided_sessions(touched);
"""


class ReusedRequest(ValueError):
    pass


class JourneyStore:
    def __init__(self, dsn: str = "") -> None:
        self.dsn = dsn
        self.pool: Any = None
        self._init_lock = asyncio.Lock()
        # Bound the number of locks even for arbitrary invalid session ids.
        self._locks = [asyncio.Lock() for _ in range(128)]
        self._replays: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()
        self._maintenance_at = 0.0

    async def database(self) -> Any:
        if self.pool is None and self.dsn:
            import asyncpg  # type: ignore[import-untyped]

            async with self._init_lock:
                if self.pool is None:
                    pool = await asyncpg.create_pool(
                        self.dsn, min_size=1, max_size=8, command_timeout=30
                    )
                    try:
                        async with pool.acquire() as conn, conn.transaction():
                            await conn.execute("SELECT pg_advisory_xact_lock(17789192)")
                            await conn.execute(_SCHEMA)
                    except BaseException:
                        await pool.close()
                        raise
                    self.pool = pool
        return self.pool

    async def open(self, s: Session) -> None:
        if pool := await self.database():
            await pool.execute(
                "INSERT INTO guided_sessions(id,state) VALUES($1,$2::jsonb)",
                s.id,
                snapshot(s),
            )
            if time.monotonic() >= self._maintenance_at:
                self._maintenance_at = time.monotonic() + 600
                await pool.execute(
                    "DELETE FROM guided_sessions WHERE touched < now() - interval '1 day'"
                )
                await pool.execute(
                    "DELETE FROM guided_rank_spend WHERE reserved_at < now() - interval '2 days'"
                )
                await pool.execute(
                    "DELETE FROM guided_decisions WHERE created_at < now() - interval '30 days'"
                )

    async def exists(self, sid: str, sessions: Sessions) -> bool:
        if pool := await self.database():
            return bool(
                await pool.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM guided_sessions"
                    " WHERE id=$1 AND touched > now()-interval '15 minutes')",
                    sid,
                )
            )
        try:
            sessions.get(sid)
            return True
        except UnknownSession:
            return False

    async def run(
        self,
        sessions: Sessions,
        sid: str,
        key: str,
        signature: str,
        action: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        lock = self._locks[hash(sid) % len(self._locks)]
        async with lock:
            if not (pool := await self.database()):
                session = sessions.get(sid)
                held = self._replays.get((sid, key)) if key else None
                if held:
                    if held[0] != signature:
                        raise ReusedRequest()
                    return dict(json.loads(held[1]))
                view = await action()
                # The decision record is for the audit trail, not the page.
                view.pop("decision", None)
                if key:
                    # Keep only the screen state. Spoken input is ephemeral.
                    clean = {
                        k: v for k, v in view.items() if k not in ("heard", "decision")
                    }
                    self._replays[(session.id, key)] = (signature, json.dumps(clean))
                    if len(self._replays) > 4096:
                        self._replays.popitem(last=False)
                return view

            async with pool.acquire() as conn, conn.transaction():
                state = await conn.fetchval(
                    "SELECT state FROM guided_sessions WHERE id=$1 AND touched > now()-interval '15 minutes' FOR UPDATE",
                    sid,
                )
                if state is None:
                    raise UnknownSession(sid)
                if key:
                    row = await conn.fetchrow(
                        "SELECT signature,response FROM guided_requests WHERE session_id=$1 AND id=$2",
                        sid,
                        key,
                    )
                    if row:
                        if signature != row["signature"]:
                            raise ReusedRequest()
                        return dict(json.loads(row["response"]))
                session = restore(sid, state)
                sessions.restore(session)

                async def reserve() -> bool:
                    # Separate connection/transaction: a crash after sending to the
                    # provider cannot roll back the unique admission record.
                    import asyncpg

                    durable = await asyncpg.connect(self.dsn, timeout=3)
                    try:
                        return bool(
                            await durable.fetchval(
                                "INSERT INTO guided_rank_spend(session_id) VALUES($1) ON CONFLICT DO NOTHING RETURNING true",
                                sid,
                            )
                        )
                    finally:
                        await durable.close()

                token = reserve_rank.set(reserve)
                try:
                    view = await action()
                finally:
                    reserve_rank.reset(token)
                await conn.execute(
                    "UPDATE guided_sessions SET state=$2::jsonb,touched=now() WHERE id=$1",
                    sid,
                    snapshot(session),
                )
                if key:
                    clean = {
                        k: v for k, v in view.items() if k not in ("heard", "decision")
                    }
                    await conn.execute(
                        "INSERT INTO guided_requests VALUES($1,$2,$3,$4::jsonb)",
                        sid,
                        key,
                        signature,
                        json.dumps(clean),
                    )
                if decision := view.pop("decision", None):
                    await conn.execute(
                        "INSERT INTO guided_decisions(session_hash,decision)"
                        " VALUES($1,$2::jsonb)",
                        hashlib.sha256(sid.encode()).hexdigest(),
                        json.dumps(decision),
                    )
                return view
