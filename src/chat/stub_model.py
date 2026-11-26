"""A scripted text model for local staging and browser tests.

Production never sets ``WIDGET_STUB_MODEL``. The reply is fixed English, so a
double tap or a dropped socket can be shown to produce one answer and no
call to a provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from src.chat.gemini_text import ChatTurn

STUB_REPLY = (
    "You can confirm the return window on the product page. "
    "Ask me about stock or EMI and I will check this item."
)


class StubTextModel:
    """Yields one English reply in two chunks. ``calls`` counts generations."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = max(0.0, delay_s)
        self.calls = 0

    async def stream(
        self, history: list[dict[str, Any]], temperature: float | None = None
    ) -> AsyncIterator[tuple[str, ChatTurn]]:
        del history, temperature
        self.calls += 1
        turn = ChatTurn()
        turn.usage = {"promptTokenCount": 120, "candidatesTokenCount": 40}
        turn.text = STUB_REPLY
        mid = max(1, len(STUB_REPLY) // 2)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        yield STUB_REPLY[:mid], turn
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        yield STUB_REPLY[mid:], turn
