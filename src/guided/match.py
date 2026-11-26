"""Words pressing buttons, by code alone. No model, no tokens, no paise.

The owner's bar for the question phase is exact: "ஒரு பைசா கூட
செலவாகக்கூடாது" - forty minutes of questions must cost nothing. The
browser hears the customer for free (its own speech recognition); this
module turns what it heard into the same presses a finger makes. It can
only name buttons that exist, so it inherits every guarantee a tap has.

Matching is deliberately plain: synonyms, substrings and the Tamil number
reader the Live path already trusts. A sentence that matches nothing is a
recorded "சொல்லுங்க இல்லன்னா தொடுங்க" - never a model call. The paid ear
(`hear.py`, flash-lite) stays behind GUIDED_MIC for the phone line, where
there is no browser to listen for free.
"""

from __future__ import annotations

import re

from src.agent.tamil_numbers import digitise
from src.guided.engine import (
    BUDGET_UP,
    CONFIRM,
    EXCLUDE,
    FEATURE,
    PREFER,
    USE_LEVELS,
)
from src.guided.hear import (
    ASK,
    BACK,
    CHEAPER,
    COMMAND,
    LIST,
    MORE,
    NEXT,
    PICK,
    RESTART,
    Press,
)
from src.guided.script import (
    ANY,
    BRAND,
    BUDGET,
    BY_SHELF,
    ENGLISH,
    LANGUAGE,
    PRIORITY,
    PURPOSE,
    Priority,
    SCRIPTS,
    SHELF,
    TAMIL,
    Script,
)

_TAMIL_LETTERS = re.compile(r"[஀-௿]")
_SPACES = re.compile(r"[^\w஀-௿]+", re.UNICODE)
_NUMBER = re.compile(r"\d[\d,]*")

# ── the vocabulary ──────────────────────────────────────────────────────────
#
# Spoken Tamil arrives in Tamil script OR romanised, so both are listed.
# These are words people actually say in a shop, not translations.

SHELF_WORDS: dict[str, tuple[str, ...]] = {
    "phone": ("போன்", "ஃபோன்", "மொபைல்", "செல்", "phone", "mobile", "cell", "smartphone"),
    "tv": ("டிவி", "தொலைக்காட்சி", "tv", "television", "telly"),
    "laptop": ("லேப்டாப்", "லாப்டாப்", "laptop", "notebook"),
    "ac": ("ஏசி", "எசி", "ac", "aircondition", "கூலர்"),
    "fridge": ("ஃப்ரிட்ஜ்", "பிரிட்ஜ்", "குளிர்சாதன", "fridge", "refrigerator"),
    "washing machine": ("வாஷிங்", "சலவை", "washing", "washer"),
    "tablet": ("டேப்", "டேப்லெட்", "tab", "tablet", "ipad", "ஐபேட்"),
    "watch": ("வாட்ச்", "கடிகார", "watch", "smartwatch"),
    "headphones": (
        "ஹெட்",
        "இயர்",
        "head",
        "ear",
        "earphone",
        "headphone",
        "earbuds",
        "buds",
    ),
    "speaker": ("ஸ்பீக்கர்", "speaker", "சவுண்ட்பார்", "soundbar"),
    "powerbank": ("பவர்", "power", "powerbank"),
    "charger": ("சார்ஜர்", "charger", "adapter", "அடாப்டர்"),
    "case": ("கேஸ்", "கவர்", "case", "cover"),
    "geyser": ("கீசர்", "ஹீட்டர்", "geyser", "heater"),
    "kitchen": ("கிச்சன்", "மிக்ஸி", "kitchen", "mixer", "grinder", "கிரைண்டர்"),
    "home appliance": ("வீட்டு", "அயர்ன்", "iron", "வாக்குவம்", "vacuum"),
}

BRAND_WORDS: dict[str, tuple[str, ...]] = {
    "samsung": ("சாம்சங்", "சம்சுங்"),
    "apple": ("ஆப்பிள்", "ஐபோன்", "iphone"),
    "vivo": ("விவோ", "வீவோ"),
    "oppo": ("ஒப்போ", "ஓப்போ"),
    "realme": ("ரியல்மி", "ரியல்மீ"),
    "nokia": ("நோக்கியா",),
    "poco": ("போகோ", "போக்கோ"),
    "redmi": ("ரெட்மி",),
    "nothing": ("நத்திங்",),
    "oneplus": (
        "ஒன்ப்ளஸ்",
        "ஒன் பிளஸ்",
    ),
    "hmd": ("எச்எம்டி",),
    "lg": ("எல்ஜி",),
    "haier": ("ஹையர்", "ஹயர்"),
    "sony": ("சோனி",),
    "jbl": ("ஜேபிஎல்",),
    "boat": ("போட்",),
    "zebronics": ("ஜீப்ரோனிக்ஸ்",),
    "whirlpool": ("வர்ல்பூல்",),
    "ifb": ("ஐஎஃப்பி",),
    "bosch": ("போஷ்",),
}

USE_WORDS: dict[str, tuple[str, ...]] = {
    "study": ("படிப்பு", "படிக்க", "ஸ்டூடன்ட்", "study", "student", "school", "college"),
    "work": ("வேலை", "ஆபீஸ்", "ப்ரொஃபஷன்", "work", "office", "professional", "job"),
    "fun": ("பொழுதுபோக்கு", "என்டர்டெயின்", "entertainment", "fun", "டைம்பாஸ்"),
    "family": (
        "வீட்டு",
        "ஃபேமிலி",
        "family",
        "home",
        "அம்மா",
        "அப்பா",
        "பாட்டி",
        "தாத்தா",
        "பெற்றோர்",
        "mother",
        "mom",
        "father",
        "dad",
        "parents",
        "grandma",
        "grandpa",
    ),
    "school": ("பள்ளி", "ஸ்கூல்", "school", "plus two", "+2"),
    "college": ("காலேஜ்", "கல்லூரி", "college", "டிகிரி", "degree"),
    "course": ("இன்ஜினியரிங்", "மெடிக்கல்", "engineering student", "medical", "டாக்டர்"),
    "it": ("ஐடி", "சாஃப்ட்வேர்", "software", "developer", "டெவலப்பர்"),
    "business": ("பிசினஸ்", "பிஸினஸ்", "business", "கடை", "சொந்த"),
    "field": (
        "சைட்",
        "site engineer",
        "ஃபீல்ட்",
        "field",
        "வெளியூர்",
        "டிரைவிங்",
        "travel",
        "டூர்",
    ),
    "dev": ("டெவலப்", "டெஸ்டிங்", "develop", "coding", "testing", "கோடிங்"),
    "meetings": ("மீட்டிங்", "கால்", "மெயில்", "meeting", "mail", "zoom"),
    "light": ("லைட்", "light", "சாதாரண", "normal", "சும்மா"),
    "gaming": ("கேம்", "game", "gaming", "pubg", "freefire", "பப்ஜி"),
    "steady": (
        "stabili",
        "steady video",
        "moving video",
        "video while walking",
        "walking video",
        "வீடியோ ஆடாம",
        "வீடியோ எடுக்",
        "நடந்துக்கிட்டே",
        "நடக்கும்போது வீடியோ",
    ),
    "hdmi": ("hdmi", "எச்டிஎம்ஐ"),
    "bluetooth": ("bluetooth", "ப்ளூடூத்"),
    "portable": ("portable", "பேட்டரியில்", "battery powered"),
    "camera": ("கேமரா", "camera", "போட்டோ", "photo", "ரீல்", "reel", "செல்ஃபி", "selfie"),
    "movies": ("சினிமா", "படம்", "movie", "ott", "சீரியல்", "youtube", "யூடியூப்"),
    "battery": ("பேட்டரி", "battery", "சார்ஜ்", "charge"),
    "performance": ("வேகம்", "ஸ்பீட்", "speed", "fast", "ராம்", "ram", "performance"),
    "storage": ("ஸ்டோரேஜ்", "storage", "மெமரி", "memory", "ஜிபி", "space"),
    "value": ("மதிப்பு", "value", "வொர்த்", "worth", "பெஸ்ட்"),
    "room": ("ரூம்", "room", "பெட்ரூம்"),
    "hall": ("ஹால்", "hall", "டிவியோட"),
    "party": ("பார்ட்டி", "party", "வெளில", "அவுட்டோர்", "outdoor"),
    "bedroom": ("பெட்ரூம்", "bedroom"),
    "calls": ("கால்", "call", "பேச"),
    "music": ("பாட்டு", "மியூசிக்", "music", "song"),
    "gym": ("ஜிம்", "gym", "ஓட்டம்", "run", "வொர்க்அவுட்", "workout"),
}

CARD_WORDS: dict[str, tuple[str, ...]] = {
    "why": ("why this", "why did", "ஏன்"),
    "price": ("current price", "how much is", "விலை என்ன"),
    "offers": ("offer", "ஆஃபர்"),
    "emi": ("எமி", "இஎம்ஐ", "emi", "மாதம்", "installment", "தவணை"),
    "where": ("எங்க", "கடை", "where", "shop", "store", "கிடைக்கும்", "பிராஞ்ச", "branch"),
    "warranty": ("வாரண்டி", "கேரண்டி", "warranty", "guarantee"),
    "colours": ("கலர்", "நிறம்", "colour", "color"),
    "box": ("பாக்ஸ்", "box", "சார்ஜர் வருமா", "உள்ள என்ன"),
    "dealer": ("டீலர்", "dealer", "ஆள்கிட்ட", "சேல்ஸ்"),
}

COMMAND_WORDS: dict[str, tuple[str, ...]] = {
    NEXT: ("வேற", "வேறு", "another", "next", "இன்னொண்ணு", "மாத்தி"),
    LIST: ("லிஸ்ட்", "list", "எல்லாம் காட்டு", "மொத்தமா"),
    MORE: ("இன்னும்", "more", "மேலும்"),
    CHEAPER: ("கம்மி", "குறைவா", "cheap", "மலிவு", "விலை குறை"),
    BACK: ("பின்னாடி", "back", "முந்தைய", "போனத"),
    RESTART: ("முதல்ல இருந்து", "restart", "ஆரம்பத்த", "start again", "ரீசெட்"),
}

YES_WORDS = (
    "சரி",
    "ஆமா",
    "ஆம்",
    "ஓகே",
    "ok",
    "okay",
    "yes",
    "sari",
    "aama",
    "correct",
    "அதேதான்",
)
ANY_WORDS = (
    "பரவாயில்ல",
    "எதுவானாலும்",
    "எதுவும்",
    "any",
    "கவலை இல்ல",
    "doesn't matter",
    "எது வேணாலும்",
    "விருப்பம் இல்ல",
)
# "I take videos at construction sites": a video word and a site word in one
# sentence is the site-video use, whatever order they come in. Either alone
# is not - "video" alone is entertainment, "site" alone is field work.
VIDEO_WORDS = ("வீடியோ", "video", "ரெக்கார்ட்", "record", "shoot", "ஷூட்")
SITE_WORDS = ("சைட்", "site", "construction", "கட்டுமான", "கன்ஸ்ட்ரக்ஷன்", "கட்டிட")
MOVEMENT_WORDS: dict[str, tuple[str, ...]] = {
    "moving": ("நடந்து", "நடக்கும்போது", "walking", "moving", "while walking"),
    "still": ("ஒரே இடத்", "one spot", "one place", "tripod", "fixed", "நின்னு"),
}

# What an "any" word can be about. "எந்த பிராண்டும் பரவாயில்ல" is about the
# brand and says nothing of the budget - the owner's own test pressed
# budget=any from it, swallowed the budget said next, and woke the model.
BRAND_NOUNS = ("பிராண்ட", "brand", "கம்பெனி", "company")
BUDGET_NOUNS = ("பட்ஜெ", "budget", "விலை", "price", "காசு", "பணம்", "ரேட்", "rate")
# Said right after a brand, these take the brand back: "Samsung கட்டாயமில்லை"
# is any brand, not Samsung. (Excluding one brand is not modelled yet, so
# "Samsung வேண்டாம்" also reads as any brand - better than pressing Samsung.)
NOT_REQUIRED = (
    "கட்டாயமில்ல",
    "கட்டாயம் இல்ல",
    "தேவையில்ல",
    "தேவை இல்ல",
    "not necessary",
    "not needed",
    "not mandatory",
    "not required",
    "no need",
    "not a must",
)
NOT_REQUIRED_REACH = 25
# "Samsung வேண்டாம்" / "Samsung தவிர" / "no Samsung": that brand is taken
# off, every other brand stays.
EXCLUDE_WORDS = (
    "வேண்டாம்",
    "வேணாம்",
    "தவிர",
    "except",
    "not",
    "no",
    "dont want",
    "don t want",
    "avoid",
)
# "Vivo இருந்தா நல்லது": first in the list, nothing hidden.
PREFER_WORDS = (
    "இருந்தா நல்லது",
    "இருந்தா நல்லா",
    "முடிஞ்சா",
    "விருப்பம்",
    "பிடிக்கும்",
    "prefer",
    "preferably",
    "if possible",
)
# "Around ₹25,000": a band ten per cent either side, not a ceiling.
AROUND_WORDS = ("சுமார்", "கிட்டத்தட்ட", "அளவுல", "around", "about", "approx", "roughly")
# Features a customer may require, by the measure the shelf keeps.
FEATURE_SAID: dict[str, tuple[str, ...]] = {
    "usb_c": ("usb c", "type c", "typec", "டைப் சி", "சி டைப்", "டைப்-சி"),
    "five_g": ("5g", "ஃபைவ் ஜி", "5 ஜி"),
    "amoled": ("amoled", "அமோலெட்", "oled"),
}
# Answers to the two questions the flow asks of its own accord.
CLASH_WORDS: dict[str, tuple[str, ...]] = {
    "other": (
        "வேற பிராண்ட்",
        "வேற brand",
        "other brand",
        "different brand",
        "another brand",
    ),
    "same": ("அதே பிராண்ட்", "இதே பிராண்ட்", "same brand", "அடுத்த விலை", "next price"),
}
RAISE_BUDGET = (
    "பட்ஜெட் கூட்ட",
    "budget கூட்ட",
    "increase budget",
    "more budget",
    "raise the budget",
)

# "ok" is "any" only right after the thing it is about - "பிராண்டும் ஓகே".
# Alone it is a yes, and a yes is decided by the question on screen.
ANY_IF_NAMED = ("ஓகே", "ok", "okay", "பரவால")
# How far, in characters, an "any" word reaches for the thing it is about.
# Browser speech arrives without commas, so nearness is the only clause.
ANY_REACH = 30

ORDINALS: dict[str, int] = {
    "முதல்": 1,
    "ஒண்ணாவது": 1,
    "first": 1,
    "ஒன்றாவது": 1,
    "ரெண்டாவது": 2,
    "இரண்டாவது": 2,
    "second": 2,
    "rendavathu": 2,
    "மூணாவது": 3,
    "மூன்றாவது": 3,
    "third": 3,
    "moonavathu": 3,
    "நாலாவது": 4,
    "நான்காவது": 4,
    "fourth": 4,
    "அஞ்சாவது": 5,
    "ஐந்தாவது": 5,
    "fifth": 5,
    "கடைசி": -1,
    "last": -1,
}


def _plain(text: str) -> str:
    """Lowercased, numbers in digits, one space between words."""
    return _SPACES.sub(" ", digitise(text).lower()).strip()


def _has(plain: str, words: tuple[str, ...]) -> bool:
    return any(
        re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", plain)
        if word.isascii() and len(word) <= 4
        else word in plain
        for word in words
    )


# The cards carry letters A, B, C, D… on screen. Spoken, a letter is only a
# card when it comes with "option" or "card" - a bare "c" is too many things.
_CARD_NOUN = r"(?:option|ஆப்ஷன்|card|கார்டு|கார்ட்)"
_LATIN_LETTER = re.compile(
    rf"(?<![a-z0-9])([a-j])\s*{_CARD_NOUN}|{_CARD_NOUN}\s*([a-j])(?![a-z0-9])"
)
_TAMIL_LETTERS_SAID = {"ஏ": 1, "பி": 2, "சி": 3, "டி": 4, "ஈ": 5}
_TAMIL_LETTER = re.compile(rf"(ஏ|பி|சி|டி|ஈ)\s*{_CARD_NOUN}")


def _letter_place(plain: str) -> int | None:
    """1 for "A option", 3 for "option c" or "சி ஆப்ஷன்"; None if no letter."""
    if found := _LATIN_LETTER.search(plain):
        letter = found.group(1) or found.group(2)
        return "abcdefghij".index(letter) + 1
    if found := _TAMIL_LETTER.search(plain):
        return _TAMIL_LETTERS_SAID[found.group(1)]
    return None


def _places(plain: str, words: tuple[str, ...]) -> list[int]:
    """Where each of these words starts, matched the way `_has` matches."""
    found: list[int] = []
    for word in words:
        if word.isascii() and len(word) <= 4:
            pattern = r"(?<![a-z])" + re.escape(word) + r"(?![a-z])"
        else:
            pattern = re.escape(word)
        found.extend(m.start() for m in re.finditer(pattern, plain))
    return found


def any_about(plain: str, current: str | None) -> frozenset[str]:
    """Which questions this sentence's "any" words answer.

    Each one belongs to the nearest brand or budget word within reach, on
    either side ("any brand", "பிராண்ட் பரவாயில்ல"). One with nothing in
    reach answers the question on screen, and only if that is the brand or
    the budget - never both, never a question nobody was asked.
    """
    topics = [(at, BRAND) for at in _places(plain, BRAND_NOUNS)] + [
        (at, BUDGET) for at in _places(plain, BUDGET_NOUNS)
    ]
    about: set[str] = set()
    for at in _places(plain, ANY_WORDS):
        # Tamil names the thing first - "பிராண்டும் பரவாயில்ல" - so the
        # nearest noun BEFORE the word wins; only with none before it does
        # one after count, as in English "any brand".
        before = [(at - where, topic) for where, topic in topics if where < at]
        after = [(where - at, topic) for where, topic in topics if where > at]
        before = [pair for pair in before if pair[0] <= ANY_REACH]
        after = [pair for pair in after if pair[0] <= ANY_REACH]
        if before:
            about.add(min(before)[1])
        elif after:
            about.add(min(after)[1])
        elif current in (BRAND, BUDGET):
            about.add(current)
    for at in _places(plain, ANY_IF_NAMED):
        before = [(at - where, topic) for where, topic in topics if where < at]
        before = [pair for pair in before if pair[0] <= ANY_REACH]
        if before:
            about.add(min(before)[1])
    return frozenset(about)


# The language NAMED wins over the script it arrived in: a Tamil recogniser
# writes "English" as "இங்கிலீஷ்", which is a request for English.
_ASKS_ENGLISH = ("english", "இங்கிலீஷ்", "இங்கிலிஷ்", "ஆங்கில")
_ASKS_TAMIL = ("tamil", "தமிழ்", "thamizh", "tamizh")


def _spoken_language(text: str) -> str:
    plain = text.lower()
    if any(word in plain for word in _ASKS_ENGLISH):
        return ENGLISH
    if any(word in plain for word in _ASKS_TAMIL):
        return TAMIL
    return TAMIL if _TAMIL_LETTERS.search(text) else ENGLISH


_ENGLISH_TENS = {
    "ten": 10,
    "fifteen": 15,
    "twenty": 20,
    "twentyfive": 25,
    "thirty": 30,
    "thirtyfive": 35,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "five": 5,
    "two": 2,
    "three": 3,
    "four": 4,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "one": 1,
    "hundred": 100,
}
_HALVES = {"ஒன்றரை": 1.5, "ஒன்னரை": 1.5, "ரெண்டரை": 2.5, "மூணரை": 3.5, "அரை": 0.5}


def rupees_in(plain: str) -> list[int]:
    """Every rupee figure a sentence states, spoken any of the shop's ways.

    "முப்பதாயிரத்துக்குள்ள", "thirty thousand", "ஒன்றரை லட்சம்", "30k",
    "25 ஆயிரம்" - all of them land as numbers. The Tamil number reader does
    the fused forms; this adds the case endings it leaves attached, the
    digit-times-unit forms, the halves and the English tens.
    """
    # Colloquial fused tens the number reader does not know.
    for spoken_word, tens in (
        ("பதினைஞ்ச", "15 "),
        ("இருபத்தைஞ்ச", "25 "),
        ("முப்பத்தைஞ்ச", "35 "),
        ("நாப்பத்தைஞ்ச", "45 "),
    ):
        plain = plain.replace(spoken_word, tens)
    # "ஆயிரத்துக்குள்ள" is "ஆயிரம்" wearing a case ending.
    unfused = re.sub(r"ாயிரத([்a-z\u0b80-\u0bff]*)", "ாயிரம் ", plain)
    unfused = re.sub(r"லட்சத([்\u0b80-\u0bff]*)", "லட்சம் ", unfused)
    unfused = _plain(unfused)
    for half, portion in _HALVES.items():
        unfused = unfused.replace(half + " ", f"{portion} ").replace(
            half, f"{portion} "
        )
    for word, english_value in sorted(
        _ENGLISH_TENS.items(), key=lambda kv: -len(kv[0])
    ):
        unfused = re.sub(rf"\b{word}\b", str(english_value), unfused)

    unfused = re.sub(
        r"\b([2-9]0) ([1-9])(?= (?:thousand|lakh))",
        lambda m: str(int(m[1]) + int(m[2])),
        unfused,
    )

    figures: list[int] = []
    for found in re.finditer(
        r"(\d[\d,]*(?:\.\d+)?)\s*(k\b|ஆயிரம்|1000\b|thousand|லட்சம்|lakh|லட்ச)?",
        unfused,
    ):
        amount = float(found.group(1).replace(",", ""))
        unit = found.group(2) or ""
        if unit in ("k", "ஆயிரம்", "1000", "thousand"):
            amount *= 1_000
        elif unit:
            amount *= 100_000
        figures.append(int(amount))
    return figures


_ABOVE_WORDS = ("மேல", "மேலே", "above", "over", "starting", "முதல் விலை")


def _budget_press(script: Script, plain: str) -> Press | None:
    """A rupee figure lands in its band; "எதுவானாலும்" is the any button.

    "முப்பதாயிரத்துக்குள்ள" is a CEILING: thirty thousand belongs to the
    band that ends at it, not the one that starts there. Only "மேல" words
    read the figure as a floor.
    """
    # Storage, battery capacity and camera resolution are not rupee amounts.
    money = re.sub(
        r"\b[0-9]+(?:[.,][0-9]+)?\s*(?:gb|tb|mb|mah|mp|hz|watts?|wh|ஜிபி)\b", " ", plain
    )
    amounts = [amount for amount in rupees_in(money) if 300 <= amount <= 999_999_999]
    if not amounts:
        return None
    if len(amounts) == 2 and _has(plain, ("between", " to ", " முதல் ", "range")):
        low, high = sorted(amounts)
        return Press(BUDGET, f"range:{low}-{high}")
    if len(amounts) == 1 and _has(plain, AROUND_WORDS):
        # "சுமார் ₹25,000": ten per cent either side, to the nearest ₹500.
        around = amounts[0]
        low = int(round(around * 0.9 / 500) * 500)
        high = int(round(around * 1.1 / 500) * 500)
        return Press(BUDGET, f"range:{low}-{high}")
    floor = _has(plain, _ABOVE_WORDS) and not _has(
        plain, ("not over", "not above", "no more", "maximum", "தாண்ட")
    )
    return Press(BUDGET, f"{'floor' if floor else 'cap'}:{max(amounts)}")


def _against(labels: dict[str, tuple[str, ...]], wanted: str, plain: str) -> str | None:
    for identity, words in labels.items():
        if identity == wanted and _has(plain, words):
            return identity
    return None


def named_priorities(script: Script, text: str) -> list[Priority]:
    plain = _plain(text)
    return [p for p in script.priorities if _has(plain, USE_WORDS.get(p.id, ()))]


def matches(
    text: str,
    *,
    shelf: str | None,
    language_open: bool,
    answered: frozenset[str],
    shown: int,
    picked: bool,
    confirming: bool = False,
    current: str | None = None,
) -> list[Press]:
    """Every press this sentence makes, in the order a salesperson takes them.

    `answered` is what is already settled, so "under thirty thousand" said
    again does not re-press the budget. Pure, and tested like arithmetic.
    """
    plain = _plain(re.sub(r"(?<=\d),(?=\d)", "", text))
    if not plain:
        return []
    presses: list[Press] = []

    if language_open:
        presses.append(Press(LANGUAGE, _spoken_language(text)))

    script = BY_SHELF.get(shelf or "")
    if shelf is None:
        for candidate in SCRIPTS:
            if _has(plain, SHELF_WORDS.get(candidate.shelf, ())):
                presses.append(Press(SHELF, candidate.shelf))
                script = candidate
                break

    if script is not None:
        any_for = any_about(plain, current)
        named_brand = next(
            (b for b, extra in BRAND_WORDS.items() if b in plain or _has(plain, extra)),
            None,
        )
        if named_brand is not None:
            named = _places(plain, (named_brand, *BRAND_WORDS[named_brand]))

            def near(words: tuple[str, ...], before: int = 0) -> bool:
                return any(
                    -before <= at - where <= NOT_REQUIRED_REACH
                    for where in named
                    for at in _places(plain, words)
                )

            taken_back = near(NOT_REQUIRED)
            excluded = not taken_back and near(EXCLUDE_WORDS, before=8)
            preferred = not taken_back and not excluded and near(PREFER_WORDS)
            if excluded or preferred:
                presses.append(Press(EXCLUDE if excluded else PREFER, named_brand))
                if BRAND not in answered:
                    presses.append(Press(BRAND, ANY))
            elif BRAND not in answered:
                presses.append(Press(BRAND, ANY if taken_back else named_brand))
        elif BRAND not in answered and BRAND in any_for:
            presses.append(Press(BRAND, ANY))
        if BUDGET not in answered:
            if press := _budget_press(script, plain):
                presses.append(press)
            elif BUDGET in any_for:
                presses.append(Press(BUDGET, ANY))
        if PURPOSE not in answered:
            for use in script.uses:
                if _has(plain, USE_WORDS.get(use.id, ())):
                    presses.append(Press(PURPOSE, use.id))
                    break
        site_video = next(
            (
                (use, child)
                for use in script.uses
                for child in use.then
                if child.id == "site_video"
            ),
            None,
        )
        said_site_video = (
            site_video is not None
            and _has(plain, VIDEO_WORDS)
            and _has(plain, SITE_WORDS)
        )
        if site_video is not None and said_site_video:
            parent, child = site_video
            if PURPOSE not in answered:
                presses.append(Press(PURPOSE, parent.id))
            presses.append(Press(USE_LEVELS[0], child.id))
            for grand_id, words in MOVEMENT_WORDS.items():
                if _has(plain, words):
                    presses.append(Press(USE_LEVELS[1], grand_id))
                    break
        elif site_video is not None and current == USE_LEVELS[1]:
            # The movement question on screen, answered on its own.
            for grand_id, words in MOVEMENT_WORDS.items():
                if _has(plain, words):
                    presses.append(Press(USE_LEVELS[1], grand_id))
                    break
        level_questions = iter(USE_LEVELS)
        for use in script.uses if not said_site_video else ():
            for child in use.then:
                if _has(plain, USE_WORDS.get(child.id, ())):
                    presses.append(
                        Press(next(level_questions, USE_LEVELS[-1]), child.id)
                    )
                    for grand in child.then:
                        if _has(plain, USE_WORDS.get(grand.id, ())):
                            presses.append(
                                Press(next(level_questions, USE_LEVELS[-1]), grand.id)
                            )
                            break
                    break
            else:
                continue
            break
        if PRIORITY not in answered and not said_site_video:
            # A site-video sentence is answered by the movement question,
            # not by guessing the priority from the word "video".
            for priority in script.priorities:
                if _has(plain, USE_WORDS.get(priority.id, ())):
                    presses.append(Press(PRIORITY, priority.id))
                    break
        if script.shelf == "phone":
            for feature, words in FEATURE_SAID.items():
                if _has(plain, words):
                    presses.append(Press(FEATURE, feature))
        if current == "clash":
            for choice, words in CLASH_WORDS.items():
                if _has(plain, words):
                    presses.append(Press("clash", choice))
                    break
        if shown and _has(plain, RAISE_BUDGET):
            presses.append(Press("relax", "budget"))
        if BUDGET_UP not in answered and _has(
            plain, ("மேல போனாலும்", "மேலயும்", "a little above is fine")
        ):
            presses.append(Press(BUDGET_UP, "yes"))
        # At the read-back, "change" with nothing concrete named steps back
        # one question. Named changes - "இல்ல, முப்பதாயிரத்துக்குள்ள" - were
        # already caught above and re-press their own question instead.
        if confirming and not presses and _has(plain, ("மாத்த", "மாற்ற", "change")):
            presses.append(Press(CONFIRM, "change"))

    if shown:
        lettered = _letter_place(plain)
        if lettered is not None:
            # "C option-க்கு EMI இருக்கா?" - the third card ON SCREEN, A to J.
            if 1 <= lettered <= shown:
                presses.append(Press(PICK, str(lettered)))
        else:
            for word, place in ORDINALS.items():
                if word in plain:
                    ref = shown if place == -1 else place
                    if 1 <= ref <= shown:
                        presses.append(Press(PICK, str(ref)))
                    break

    if picked or (shown and any(p.question == PICK for p in presses)):
        for key, words in CARD_WORDS.items():
            if _has(plain, words):
                presses.append(Press(ASK, key))
                break

    for command, words in COMMAND_WORDS.items():
        if _has(plain, words):
            # "இன்னும் காட்டு" must not also read as "வேற" etc: one command.
            presses.append(Press(COMMAND, command))
            break

    if not presses and _has(plain, YES_WORDS):
        # "சரி" with a suggestion on screen accepts it - pressed as the
        # first button of whatever question is up, decided by the flow.
        presses.append(Press("__yes__", "__yes__"))
    return presses
