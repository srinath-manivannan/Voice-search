"""In-memory audio transports, so session logic can be tested with no device.

These are not toys - the session tests depend on being able to assert exactly
what was written and exactly when it was discarded, which a real speaker cannot
tell you.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.agent.tools import estate as estate_api


class FakeSource:
    """Yields the frames it was constructed with, then stops.

    `keep_open` models a microphone that is still live after those frames. A
    real one does not hang up mid-call, and `Conversation.run` cancels the
    other pump as soon as either side ends - so a source that stops early will
    cut the model off before it has finished speaking.
    """

    def __init__(
        self, frames: list[bytes], sample_rate: int = 16000, keep_open: bool = False
    ) -> None:
        self._frames = list(frames)
        self._sample_rate = sample_rate
        self._keep_open = keep_open
        self.closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame
            await asyncio.sleep(0)
        if self._keep_open:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class FakeSink:
    """Records writes, and records what was still unheard at each flush."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self._sample_rate = sample_rate
        self.written: list[bytes] = []
        self.flushes = 0
        self.discarded: list[bytes] = []
        self.closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    async def flush(self) -> None:
        self.flushes += 1
        self.discarded.extend(self.written)
        self.written = []

    async def close(self) -> None:
        self.closed = True


# ── the catalogue and the estate, without a network ───────────────────────
#
# Moved here out of tests/test_estate.py when that file passed 500 lines.
# Three test modules build the same ToolSession now, and three private copies
# of these would be three chances for them to disagree about what the
# upstream answers.


class FakeEstate:
    """A backend with one payload per item code. No network, no money."""

    def __init__(self, by_code: dict[str, dict]) -> None:
        self._by_code = by_code
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    async def product(self, item_code: str, sections: tuple[str, ...]) -> dict:
        self.asked.append((item_code, sections))
        if item_code not in self._by_code:
            raise estate_api.EstateUnavailable(f"no payload for {item_code}")
        return self._by_code[item_code]


def priced(item_code: str, selling: int) -> dict:
    return {
        "item_code": item_code,
        "sections": {
            "pricing": {
                "status": "ok",
                "data": {"price": selling, "mrp": selling},
            }
        },
        "summary": {"selling_price": selling, "list_price": selling},
    }


def hits(*documents: dict) -> dict:
    return {
        "results": [
            {
                "found": len(documents),
                "hits": [{"document": d} for d in documents],
            }
        ]
    }


def document(item_code: str, name: str, price: int) -> dict:
    return {
        "item_code": item_code,
        "name": name,
        "price": price,
        "brand_name": "Samsung",
        "availability": "In Stock",
        "stock_status": "In Stock",
        "variant_status": 1,
    }


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakePoster:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def post(self, url, *, json=None, headers=None):  # noqa: A002
        return FakeResponse(self._payload)


def catalogue_of(*documents: dict):
    from src.agent.tools.catalogue import Catalogue

    payload = hits(*documents)
    return Catalogue(
        "https://example.invalid", "token", poster_factory=lambda: FakePoster(payload)
    )
