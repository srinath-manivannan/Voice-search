"""Score a voice route without placing a paid call.

``python -m src.tools.measure_route --route C --fixture`` reads a JSON list of
turns and writes a gate report. Live audio is not contacted. The go/no-go
check is the one-sided 95% Clopper-Pearson bound on the share of calls that
cost more than ₹2 per active minute.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _binom_pmf(n: int, k: int, p: float) -> float:
    if p <= 0:
        return 1.0 if k == 0 else 0.0
    if p >= 1:
        return 1.0 if k == n else 0.0
    return math.exp(_log_choose(n, k) + k * math.log(p) + (n - k) * math.log(1 - p))


def _cdf_leq(k: int, n: int, p: float) -> float:
    return sum(_binom_pmf(n, i, p) for i in range(k + 1))


def clopper_pearson_upper(exceedances: int, calls: int, alpha: float = 0.05) -> float:
    """One-sided upper bound on the exceedance rate.

    The bound is the smallest rate whose lower tail, P(X <= exceedances), is
    at most ``alpha``. Fewer than 5% above ₹2 is the claim the pilot has to
    support.
    """
    if calls <= 0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _cdf_leq(exceedances, calls, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def passes_tail(exceedances: int, calls: int) -> bool:
    """At 300 calls, 8 exceedances pass and 9 do not."""
    if calls < 300:
        return False
    return clopper_pearson_upper(exceedances, calls) < 0.05


@dataclass
class TurnScore:
    route: str
    inr_per_active_min: float
    factual_error: bool
    budget_breach: bool
    false_out_of_stock: bool
    english_display: bool
    missed_barge: bool


def score_turn(turn: dict[str, Any]) -> TurnScore:
    """Fixture scoring. Prices, stock and the budget are compared to the fixture, not invented."""
    said = str(turn.get("display") or "")
    price = turn.get("quoted_price")
    truth = turn.get("price")
    stock = str(turn.get("stock_state") or "")
    said_stock = str(turn.get("said_stock") or "")
    false_oos = stock == "unconfirmed" and "out of stock" in said_stock.lower()
    breach = (
        bool(turn.get("budget"))
        and isinstance(price, int)
        and price > int(turn["budget"])
    )
    if turn.get("budget_consented"):
        breach = False
    wrong_price = isinstance(price, int) and isinstance(truth, int) and price != truth
    english = not any("\u0b80" <= ch <= "\u0bff" for ch in said)
    return TurnScore(
        route=str(turn.get("route") or "C"),
        inr_per_active_min=float(turn.get("inr_per_active_min") or 0),
        factual_error=wrong_price or false_oos,
        budget_breach=breach,
        false_out_of_stock=false_oos,
        english_display=english,
        missed_barge=bool(turn.get("missed_barge")),
    )


def evaluate(turns: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [score_turn(turn) for turn in turns]
    calls = len(scored)
    exceedances = sum(1 for turn in scored if turn.inr_per_active_min > 2)
    factual = sum(1 for turn in scored if turn.factual_error)
    return {
        "calls": calls,
        "exceedances": exceedances,
        "upper_rate": round(clopper_pearson_upper(exceedances, calls), 4)
        if calls
        else 1,
        "tail_pass": passes_tail(exceedances, calls),
        "factual_errors": factual,
        "budget_breaches": sum(1 for turn in scored if turn.budget_breach),
        "false_out_of_stock": sum(1 for turn in scored if turn.false_out_of_stock),
        "english_display": all(turn.english_display for turn in scored)
        if scored
        else False,
        "missed_barges": sum(1 for turn in scored if turn.missed_barge),
        "go": (
            passes_tail(exceedances, calls)
            and factual == 0
            and all(turn.english_display for turn in scored)
            and not any(
                turn.budget_breach or turn.false_out_of_stock for turn in scored
            )
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a voice route from a fixture file"
    )
    parser.add_argument("--route", choices=("A", "B", "C"), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    turns = json.loads(args.fixture.read_text(encoding="utf-8"))
    for turn in turns:
        turn.setdefault("route", args.route)
    report = evaluate(turns)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if report["go"] or report["calls"] < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
