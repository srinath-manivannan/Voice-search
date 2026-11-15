"""Tamil number words, written back as digits.

The cascade's ear transcribes into Tamil script and writes numbers the way
Tamil says them. Measured against the live endpoint on 2026-11-15:

    sent   Enakku 20000 kulla ... 25000 varaikkum okay.
    heard  எனக்கு இருபதாயிரம் குள்ள ... இருபத்தி ஐந்தாயிரம் வரைக்கும் ஓகே.

`இருபதாயிரம்` is twenty thousand and there is not a digit in it, so
`prefetch.py` - which reads a budget with a regular expression over digit
runs - finds nothing and the prefetch never fires. The brain reads it
perfectly well; a regular expression never will.

So this rewrites the words into digits BEFORE the patterns run, rather than
adding a second way to read a budget. `இருபதாயிரம் குள்ள` becomes
`20000 குள்ள`, which `_CEILING_AFTER` already matched before any of this
existed, and every rule about floors, ceilings and ranges stays in one place.

## Why the stems look truncated

Tamil compounds fuse. இருபது + ஆயிரம் is written இருபதாயிரம், and what is
left after removing the scale word is இருபத் - not இருபது. So the table
below is keyed on the fused stem, which is the form that actually appears,
and the standalone words are listed separately.

## What it deliberately does not do

Fractions - அரை (half), ஒன்றரை (one and a half) - are not read. "ஒன்றரை
லட்சம்" is a hundred and fifty thousand and this returns nothing for it,
which leaves the budget unread rather than read as 100,000. A budget that is
wrong by fifty thousand is worse than a budget that is missing, because the
missing one still has the model's own reading of the sentence behind it.
"""

from __future__ import annotations

import re

# Scale words standing on their own. ஆயிரத்து is the form used when
# something follows - "இருபதாயிரத்து ஐநூறு", 20,500.
SCALES: dict[str, int] = {
    "ஆயிரத்து": 1000,
    "ஆயிரம்": 1000,
    "லட்சத்து": 100_000,
    "லட்சம்": 100_000,
    "லச்சம்": 100_000,
}

# The same words once they have fused onto a number, where the initial ஆ has
# become the vowel sign ா on the last letter of the stem:
#
#     ஐந்து + ஆயிரம்  ->  ஐந்தாயிரம்    ஐ ந ் த ா ய ி ர ம ்
#
# So the token does not contain the letter ஆ at all, and stripping "ஆயிரம்"
# finds nothing. The first version of this file did exactly that and matched
# none of the four forms the ear actually produced.
# Only the vowel-sign forms. A bare லட்சம் belongs in SCALES above: listing
# it here made "ஒரு லட்சம்" two hundred thousand, because the word matched
# with an empty stem, the empty stem means one, and the ஒரு before it had
# already been counted.
FUSED_SCALES: dict[str, int] = {
    "ாயிரத்து": 1000,
    "ாயிரம்": 1000,
}

# What is left of a number word once the fused scale is taken off the end.
# The final consonant keeps no pulli, because the vowel sign that replaced it
# went with the scale word: இருபது -> இருபத, ஐந்து -> ஐந்த, பத்து -> பத்த.
FUSED: dict[str, int] = {
    "": 1,  # ஆயிரம் on its own is one thousand
    "ஒர": 1,
    "ஒன்ற": 1,
    "இரண்ட": 2,
    "ரெண்ட": 2,
    "மூவ": 3,
    "மூண": 3,
    "மூன்ற": 3,
    "நான்க": 4,
    "நால": 4,
    "ஐந்த": 5,
    "அஞ்ச": 5,
    "ஆற": 6,
    "ஏழ": 7,
    "எட்ட": 8,
    "ஒன்பத": 9,
    "பத்த": 10,
    "பதினொன்ற": 11,
    "பன்னிரண்ட": 12,
    "பதின்மூன்ற": 13,
    "பதினான்க": 14,
    "பதினைந்த": 15,
    "பதினாற": 16,
    "பதினேழ": 17,
    "பதினெட்ட": 18,
    "பத்தொன்பத": 19,
    "இருபத": 20,
    "முப்பத": 30,
    "நாற்பத": 40,
    "ஐம்பத": 50,
    "அறுபத": 60,
    "எழுபத": 70,
    "எண்பத": 80,
    "தொண்ணூற": 90,
}

# Whole words, used when the number is said with a space before the scale -
# "இருபத்தி ஐந்து ஆயிரம்" - and for the tens prefix of a compound.
WORDS: dict[str, int] = {
    "ஒன்று": 1,
    "ஒரு": 1,
    "இரண்டு": 2,
    "ரெண்டு": 2,
    "மூன்று": 3,
    "மூணு": 3,
    "நான்கு": 4,
    "நாலு": 4,
    "ஐந்து": 5,
    "அஞ்சு": 5,
    "ஆறு": 6,
    "ஏழு": 7,
    "எட்டு": 8,
    "ஒன்பது": 9,
    "பத்து": 10,
    "பதினொன்று": 11,
    "பன்னிரண்டு": 12,
    "பதினைந்து": 15,
    "இருபது": 20,
    "முப்பது": 30,
    "நாற்பது": 40,
    "ஐம்பது": 50,
    "அறுபது": 60,
    "எழுபது": 70,
    "எண்பது": 80,
    "தொண்ணூறு": 90,
    "நூறு": 100,
    "இருநூறு": 200,
    "முந்நூறு": 300,
    "நானூறு": 400,
    "ஐநூறு": 500,
    # The tens prefix of a compound: இருபத்தி ஐந்தாயிரம் is (20 + 5) x 1000,
    # not 20 and then 5,000. It is a prefix and never a number on its own.
    "இருபத்தி": 20,
    "முப்பத்தி": 30,
    "நாற்பத்தி": 40,
    "ஐம்பத்தி": 50,
    "அறுபத்தி": 60,
    "எழுபத்தி": 70,
    "எண்பத்தி": 80,
    "தொண்ணூற்றி": 90,
    "இருபத்து": 20,
    "முப்பத்து": 30,
    "நாற்பத்து": 40,
    "ஐம்பத்து": 50,
}

# Quantities this cannot read, which POISON the scale word after them.
# Skipping an unknown word and reading on is what every other unknown word
# deserves and what these must never get: "ஒன்றரை லட்சம்" is a hundred and
# fifty thousand, and ignoring the ஒன்றரை leaves a bare லட்சம் that reads as
# a hundred thousand - a budget wrong by fifty thousand, which is worse than
# no budget at all. So they are named, and they take the next word with them.
FRACTIONS: frozenset[str] = frozenset(
    {
        "அரை",
        "ஒன்றரை",
        "இரண்டரை",
        "ரெண்டரை",
        "மூன்றரை",
        "மூணரை",
        "நாலரை",
        "நான்கரை",
        "ஐந்தரை",
        "அஞ்சரை",
        "காலே",
        "முக்கால்",
    }
)

# A run of Tamil letters. Punctuation and Latin text end a number.
MAX_WORDS = 4

# Below this a rewrite buys nothing: prefetch rejects anything under its own
# floor of three thousand, and "ஒரு" is an article far more often than a one.
SMALLEST_WORTH_WRITING = 100

_TAMIL_RUN = re.compile(r"[஀-௿]+")


def _fused(token: str) -> tuple[int, int] | None:
    """(units, scale) when this token is a number fused with a scale word."""
    for suffix, scale in FUSED_SCALES.items():
        if token.endswith(suffix):
            stem = token[: -len(suffix)]
            if (units := FUSED.get(stem)) is not None:
                return units, scale
    return None


def value_of(tokens: list[str]) -> int | None:
    """What a run of Tamil number words adds up to, or None if it is not one.

    Accumulates the way the language reads: units gather until a scale word
    multiplies them, and scaled groups add together. "இருபத்தி ஐந்தாயிரம்"
    is 20 gathered, then 5 gathered, then both multiplied by a thousand.
    """
    total = 0
    pending = 0
    seen = False
    for token in tokens:
        if (split := _fused(token)) is not None:
            units, scale = split
            total += (pending + units) * scale
            pending = 0
            seen = True
            continue
        if (standalone := SCALES.get(token)) is not None:
            total += max(pending, 1) * standalone
            pending = 0
            seen = True
            continue
        if (word := WORDS.get(token)) is not None:
            pending += word
            seen = True
            continue
        return None
    if not seen:
        return None
    return total + pending


def digitise(text: str) -> str:
    """The same sentence with its Tamil number words written as digits.

    Runs before `prefetch`'s own patterns, so everything downstream keeps
    reading digits and there is exactly one set of rules about what a budget
    is. A run of words that is not a number is left exactly as it was - this
    rewrites or does nothing, and never guesses.
    """
    found = list(_TAMIL_RUN.finditer(text))
    if not found:
        return text

    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(found):
        if found[index].group() in FRACTIONS:
            # Take the scale word with it, so the half is not silently
            # dropped and the rest read as a whole number.
            index += 2
            continue
        # Longest run first: "இருபத்தி ஐந்தாயிரம்" is 25,000, and reading
        # "இருபத்தி" on its own would make it 20 and then 5,000.
        longest = None
        for size in range(min(MAX_WORDS, len(found) - index), 0, -1):
            run = found[index : index + size]
            if not _adjacent(text, run):
                continue
            number = value_of([match.group() for match in run])
            # Only scaled numbers are worth rewriting. "ஒரு" is the article
            # in "ஒரு போன்" far more often than it is the number one, and
            # turning it into a 1 puts a digit next to a noun for no gain -
            # prefetch's floor is three thousand, so nothing under a hundred
            # could ever have been a budget anyway.
            if number is not None and number >= SMALLEST_WORTH_WRITING:
                longest = (size, number, run[0].start(), run[-1].end())
                break
        if longest is None:
            index += 1
            continue
        size, number, start, end = longest
        pieces.append(text[cursor:start])
        pieces.append(str(number))
        cursor = end
        index += size
    pieces.append(text[cursor:])
    return "".join(pieces)


def _adjacent(text: str, run: list[re.Match[str]]) -> bool:
    """Whether these words are separated by whitespace and nothing else.

    A comma between them ends the number. "இருபதாயிரம், முப்பதாயிரம்" is two
    budgets being compared, not fifty thousand.
    """
    return all(
        text[left.end() : right.start()].strip() == ""
        for left, right in zip(run, run[1:], strict=False)
    )
