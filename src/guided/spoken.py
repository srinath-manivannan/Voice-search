"""The sentences the guided call says about live data - built by code.

"Three phones. One: Poco M8 5G, 24,999 rupees..." is the sentence a
salesperson says after looking. It is built here from the same fields the
cards are drawn from, never written by a model: a model asked to say the
price can say a different one, and nothing downstream of a voice can take
a spoken number back.

Each sentence comes with `must_hear`: the figures that matter - prices,
EMI, how many shops - which `SpeechBook` checks the voice actually said
before anyone hears it. Model numbers ("M8", "A56") are not in it: they are
read in either script and are not what a customer pays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.guided.script import ENGLISH, rupees

# Three read aloud, however many are on screen. Five names and prices in a
# row is more than anyone holds; the screen has all five.
SPOKEN_PICKS = 3

_ORDINALS = {"ta": ("ஒண்ணு", "ரெண்டு", "மூணு"), "en": ("One", "Two", "Three")}
_BRACKETED = re.compile(r"\s*[(\[].*$")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Sentence:
    text: str
    must_hear: frozenset[float]


def spoken_name(name: str) -> str:
    """ "Poco M8 5G ( Carbon Black, 6GB-128GB )" -> "Poco M8 5G".

    The colour and memory in brackets are on the card. Read aloud they are
    four seconds of a list nobody asked for, and the memory sizes are
    figures the voice could get wrong for no benefit.
    """
    short = _BRACKETED.sub("", name).replace(",", " ")
    return _SPACE.sub(" ", short).strip() or name.strip()


def money(amount: int, lang: str) -> str:
    """24,999 ரூபாய் / 24,999 rupees - grouped the Indian way, in digits."""
    grouped = rupees(amount).lstrip("₹")
    return f"{grouped} rupees" if lang == ENGLISH else f"{grouped} ரூபாய்"


def results(picks: list[dict[str, Any]], lang: str) -> Sentence | None:
    """The top of the list, read out after "here is what suits you"."""
    if not picks:
        return None
    parts, heard = [], set()
    for ordinal, pick in zip(
        _ORDINALS.get(lang, _ORDINALS["ta"]), picks[:SPOKEN_PICKS]
    ):
        line = f"{ordinal}: {spoken_name(pick['name'])}, {money(pick['price'], lang)}."
        if pick.get("over_budget"):
            line += (
                " This one is a little over your budget."
                if lang == ENGLISH
                else " இது உங்க பட்ஜெட்டை விட கொஞ்சம் அதிகம்."
            )
        if pick.get("why"):
            line += " " + pick["why"] + "."
        parts.append(line)
        heard.add(float(pick["price"]))
    return Sentence(" ".join(parts), frozenset(heard))


def card(product: dict[str, Any], lang: str) -> Sentence:
    """The one they picked: price, where it is, and what it costs a month."""
    english = lang == ENGLISH
    parts = [f"{spoken_name(product['name'])}, {money(product['price'], lang)}."]
    heard = {float(product["price"])}

    if offer := product.get("price_with_best_offer"):
        parts.append(
            f"With the card offer, {money(offer, lang)}."
            if english
            else f"ஆஃபரோட {money(offer, lang)}."
        )
        heard.add(float(offer))

    stock = product.get("stock") or {}
    if stock.get("state") == "in" and stock.get("branches"):
        shops = int(stock["branches"])
        if english:
            parts.append(f"In stock at {shops} {'shop' if shops == 1 else 'shops'}.")
        else:
            parts.append(f"{shops} {'கடைல' if shops == 1 else 'கடைகள்ல'} ஸ்டாக் இருக்கு.")
        heard.add(float(shops))
    elif stock.get("state") == "out":
        parts.append("Out of stock right now." if english else "இப்போ ஸ்டாக் இல்ல.")

    if emi := product.get("emi"):
        parts.append(
            f"EMI from {money(emi['per_month'], lang)} a month."
            if english
            else f"EMI மாதம் {money(emi['per_month'], lang)} முதல்."
        )
        heard.add(float(emi["per_month"]))
    return Sentence(" ".join(parts), frozenset(heard))
