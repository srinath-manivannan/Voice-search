"""English on the screen, Tamil in the ear, from the same sentence.

Fixed lines already exist as a Tamil/English pair in ``script.LINES``. A
view keeps ``say`` for the recording. ``display`` is what the screen shows
and ``speech`` is what a Tamil voice would read.
"""

from __future__ import annotations

import re
from typing import Any

from src.guided.script import LINES

_TAMIL = re.compile(r"[\u0B80-\u0BFF]")
_ENGLISH = {pair[0]: pair[1] for pair in LINES.values() if pair[1]}
_TAMIL_OF = {pair[1]: pair[0] for pair in LINES.values() if pair[1]}


def english_for(text: str) -> str:
    """The English half of a fixed line, or the text when it is already English."""
    if not text:
        return ""
    if text in _ENGLISH and _ENGLISH[text]:
        return _ENGLISH[text]
    if _TAMIL.search(text):
        return "The details are on screen."
    return text


def tamil_for(text: str) -> str:
    if text in _TAMIL_OF:
        return _TAMIL_OF[text]
    return text


def annotate(view: dict[str, Any]) -> dict[str, Any]:
    """Add display and speech without changing the lines tests already read."""
    say = str(view.get("say") or "")
    view["display"] = {"lang": "en", "text": english_for(say)}
    view["speech"] = {
        "lang": "ta",
        "text": tamil_for(say),
        "ssml_hint": "numbers_as_words",
    }
    view["display_lang"] = "en"
    view["speech_lang"] = "ta"
    overview = view.get("overview")
    if isinstance(overview, dict):
        text = str(overview.get("text") or "")
        overview.setdefault("display", english_for(text))
        overview.setdefault("speech", text if _TAMIL.search(text) else tamil_for(text))
    question = view.get("question")
    if isinstance(question, dict) and question.get("ask"):
        question.setdefault("display", english_for(str(question["ask"])))
    return view
