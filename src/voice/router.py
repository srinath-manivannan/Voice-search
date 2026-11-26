"""Which voice route speaks this turn.

C is the recorded, deterministic path. B is a bounded cascade, and only when
``VOICE_ROUTE_B`` is on. A is native live audio, and only when
``VOICE_ROUTE_A`` is on. With both flags off, every turn stays on C.
"""

from __future__ import annotations

import re
from typing import Literal

Route = Literal["C", "B", "A"]

_DETERMINISTIC = re.compile(
    r"(?i)\b(option\s+[a-j]|the\s+(first|second|third|fourth|fifth)\s+one|"
    r"none of these|emi|in stock|budget|under|samsung|vivo|hello|vanakkam)\b"
)
_OPEN = re.compile(
    r"(?i)(which is better|compare|why|night|low light|better for|எது நல்ல)"
)


def choose(text: str, *, route_b: bool = False, route_a: bool = False) -> Route:
    """Pick a route. Paid routes are unreachable while their flags are off."""
    folded = text or ""
    if _OPEN.search(folded) and not _DETERMINISTIC.search(folded):
        if route_b:
            return "B"
        if route_a:
            return "A"
        return "C"
    if route_a and folded.strip().lower().startswith("premium "):
        return "A"
    return "C"
