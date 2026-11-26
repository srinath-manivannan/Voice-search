"""The worst case is reserved before a paid voice step.

Denial does not hang up. The shopper steps down a ladder that still answers
from the facts already fetched: a template, a tiny prompt, text on screen,
and only then a handover that carries the budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.web.services.conversation_store import inr_to_paise

Step = Literal["templated", "micro", "text", "handover"]
CALL_CEILING_INR = 8.0


def worst_case_inr(active_minutes: float) -> float:
    """₹2.00 per active minute already under way, plus a ₹0.50 starter, never above ₹8."""
    raw = 2.0 * max(active_minutes, 1.0) + 0.50
    return min(CALL_CEILING_INR, raw)


def worst_case_paise(active_minutes: float) -> int:
    return inr_to_paise(worst_case_inr(active_minutes))


@dataclass
class GuardDecision:
    allowed: bool
    step: Step | None = None
    note: str = ""
    slots: dict[str, object] = field(default_factory=dict)


def continuation(slots: dict[str, object]) -> list[GuardDecision]:
    """The ladder, in order, built from the slots the call already holds."""
    budget = slots.get("budget")
    budget_text = f" Your budget of ₹{budget} stays." if budget else ""
    return [
        GuardDecision(
            False,
            "templated",
            "Here is what I can confirm from the prices and stock already checked."
            + budget_text,
            dict(slots),
        ),
        GuardDecision(
            False,
            "micro",
            "A short answer from the slots only, with no history.",
            dict(slots),
        ),
        GuardDecision(
            False,
            "text",
            "The details are on screen.",
            dict(slots),
        ),
        GuardDecision(
            False,
            "handover",
            "Continue the call. Do not greet them again. "
            + (f"Budget {budget}, hard." if budget else "No budget yet."),
            dict(slots),
        ),
    ]
