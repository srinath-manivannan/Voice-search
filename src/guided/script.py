"""What the guided call asks, as data, in the customer's own language.

Every sentence the customer hears before the recommendation is in this file,
which is what makes those sentences free: fixed text can be recorded once and
played, and a model is never asked to produce it. Changing a question is an
edit here and a re-recording, not a prompt.

The shape of the conversation is the one a salesperson has. The customer
picks a language, says what they came for, then the brand they lean to, the
budget, and **what the thing is for** - and "for studies" is followed by
"studying what?", because a school bag and an engineering course want
different phones. Out of that the call suggests what should matter most and
asks whether that is right, which is the one step where the customer can
say "no - camera matters more". Only then is anything matched.

Two languages, Tamil first. The Tamil is Tamil script; the English is for
the customer who chooses it on the first screen. A sentence's recording is
played only while the screen shows exactly that sentence, in that language.
"""

from __future__ import annotations

from dataclasses import dataclass

# The answer that means "I do not mind". A value, not an absence: an
# unanswered question and a question answered "any" are different states,
# and only one of them lets the call move on.
ANY = "any"

LANGUAGE = "language"
SHELF = "shelf"
BUDGET = "budget"
BRAND = "brand"
PURPOSE = "purpose"
USE = "use"  # the follow-up: which kind of student, which kind of work
PRIORITY = "priority"

TAMIL, ENGLISH = "ta", "en"
LANGUAGES = (TAMIL, ENGLISH)

# (Tamil, English). Everything a screen shows is one of these pairs.
Words = tuple[str, str]


def word(words: Words, lang: str) -> str:
    return words[1] if lang == ENGLISH and words[1] else words[0]


@dataclass(frozen=True)
class Choice:
    """One button. `count` is how many products are really behind it."""

    id: str
    label: str
    count: int | None = None


@dataclass(frozen=True)
class Facet:
    """A question whose buttons are whatever the shelf holds today.

    `id` is also the name of the attribute it narrows on - see
    `shelf.ATTRIBUTES` - so adding a question to a shelf is one line here
    and one there, and no button can name a value the shelf does not have.
    """

    id: str
    ask: str  # a LINES key
    any_words: Words


@dataclass(frozen=True)
class Priority:
    """What matters most. It orders the answer; it never removes a product.

    `measure` is the number on a product that this is judged by. None means
    there is no such number and the order is left to price and rating.
    """

    id: str
    words: Words
    measure: str | None = None


@dataclass(frozen=True)
class Use:
    """One answer to "what is it for?", and what it implies.

    `suggests` is the priority this use points at; `spec` is the sentence
    shown when the call suggests it ("for engineering, speed and RAM").
    `ask_next` with `then` is the follow-up - a student is asked what they
    study, because the answer changes the suggestion.
    """

    id: str
    words: Words
    suggests: str = "value"
    spec: Words = ("", "")
    ask_next: str = ""  # a LINES key
    then: tuple["Use", ...] = ()
    # How the use is said back in the overview after a typed or spoken turn:
    # "சைட்ல வீடியோ எடுக்க ₹25,000க்குள் Samsung போன் பாக்குறீங்க".
    # Empty means the overview leaves it out rather than guess a phrase.
    overview: Words = ("", "")


@dataclass(frozen=True)
class Script:
    """Everything one shelf can be asked, in the order a salesperson asks."""

    shelf: str
    words: Words
    # Rupee edges between budget buttons. A band with nothing in stock is
    # never drawn, so the ladder only has to be sensible, not exact.
    edges: tuple[int, ...]
    facets: tuple[Facet, ...]
    priorities: tuple[Priority, ...] = ()
    uses: tuple[Use, ...] = ()
    # The LINES key its purpose question is asked with: a phone asks "what
    # is it for?", a speaker asks which room - the owner's own example.
    ask_purpose: str = "ask_purpose"


# ── every fixed sentence, by name ──────────────────────────────────────────
#
# The Tamil of a key that is already recorded must not change here without
# running `make guided-voice`: the recording stops being played the moment
# the text differs, and a test fails until they agree again.

LINES: dict[str, Words] = {
    # The first screen. One sentence carrying both languages, because it is
    # shown before anyone has chosen one.
    # The first thing heard: a shop answering, not a form. Shown before a
    # language is known, so the one line carries both; just speaking also
    # answers it, in whichever language was spoken.
    # The spec's own greeting, and shorter than the first draft, which ran
    # seven and a half seconds against the six the tests allow.
    "ask_language": (
        "வணக்கம்! பூர்விகாவுக்கு வரவேற்கிறோம். தமிழில் பேசலாமா, or English?",
        "",
    ),
    "ask_shelf": (
        "நான் உங்க பூர்விகா உதவியாளர். இன்னைக்கு எந்த பொருள் பாக்கணும்?",
        "I am your Poorvika assistant. What are you looking for today?",
    ),
    "ask_brand": ("எந்த பிராண்ட் வேணும்?", "Which brand do you prefer?"),
    "ask_budget": (
        "அதிகபட்சம் எவ்வளவு வரை பார்க்கலாம்?",
        "What is the most you would like to spend?",
    ),
    # Asked of someone who shoots video on a site: the answer moves the
    # ranking - walking needs stabilisation the spec sheet proves, a fixed
    # spot needs the camera.
    "ask_movement": (
        "நடந்து கொண்டே வீடியோ எடுப்பீங்களா, ஒரே இடத்திலிருந்து எடுப்பீங்களா?",
        "Will you shoot while walking, or from one spot?",
    ),
    # Said before the next question when a typed or spoken turn was
    # understood; the overview itself is on screen.
    "say_ack": ("சரிங்க.", "Sure."),
    # The brand said has nothing in the budget said: a choice, not a dead end.
    "ask_clash": (
        "இந்த பட்ஜெட்ல அந்த பிராண்ட் இல்ல. வேற பிராண்டா, அதே பிராண்டா?",
        "That brand has nothing in this budget right now. Another brand, or the same brand at its next price?",
    ),
    # The cards ran out: which one condition to change, with honest counts.
    "ask_relax": (
        "இதுக்கு மேல பொருத்தம் இல்ல. எதை மாத்தி பாக்கலாம்?",
        "Nothing more fits. Which one shall we change?",
    ),
    "ask_purpose": ("எதுக்காக வாங்கறீங்க?", "What will you mainly use it for?"),
    "ask_student": ("என்ன படிக்கறீங்க?", "What are you studying?"),
    "ask_work": ("என்ன மாதிரி வேலை?", "What kind of work do you do?"),
    "ask_it_role": ("IT-ல என்ன மாதிரி வேலை?", "What kind of IT work?"),
    "ask_fun": ("ஜாலிக்கு எதுக்கு அதிகமா யூஸ் பண்ணுவீங்க?", "What do you enjoy most?"),
    "ask_speaker_for": (
        "எதுக்காக, எந்த இடத்துக்கு வேணும்?",
        "What is it for, and for which room?",
    ),
    "ask_tv_room": ("எந்த ரூமுக்கு வேணும்?", "Which room is it for?"),
    "ask_head_for": ("எதுக்காக அதிகம் பயன்படுத்துவீங்க?", "What will you use them for most?"),
    # The honest moment the owner scripted himself: the good one is dearer.
    # The need, read back in one line before anything is recommended. The
    # line itself is fixed and recordable; the summary is composed from the
    # answers and shown under it.
    "ask_confirm": (
        "நீங்க சொன்னது இது. சரியா, ஏதாவது மாத்தணுமா?",
        "Here is what you told me. Is it right, or shall I change something?",
    ),
    "ask_budget_up": (
        "உங்க பட்ஜெட்டுக்குள்ள அதிகம் இல்ல. கொஞ்சம் மேல போனாலும் காட்டட்டுமா?",
        "There is not much inside your budget. Shall I show a little above it too?",
    ),
    "ask_spec": (
        "உங்க தேவைக்கு இது முக்கியம்னு நினைக்கிறேன். அப்படியே பாக்கட்டுமா?",
        "For your needs I would look at this. Shall I?",
    ),
    "ask_size": ("எந்த சைஸ் வேணும்?", "Which size do you want?"),
    "ask_capacity": ("எவ்வளவு கெபாசிட்டி வேணும்?", "What capacity do you need?"),
    "ask_kind": ("எந்த வகை வேணும்?", "Which type do you want?"),
    "ask_priority": ("உங்களுக்கு எது முக்கியம்?", "What matters most to you?"),
    "say_thinking": ("ஒரு நிமிஷம், பாக்குறேன்.", "One moment, let me look."),
    "say_one": (
        "இப்போ ஸ்டாக்ல இருக்கறது இது ஒண்ணு தான்.",
        "This is the only one in stock right now.",
    ),
    "say_nothing": (
        "மன்னிக்கணும், இதுல இப்போ ஸ்டாக் இல்ல. வேற ஒண்ணு பாக்கலாமா?",
        "Sorry, nothing is in stock for that. Shall we try something else?",
    ),
    "say_more": ("சரி, இதோ இன்னும் கொஞ்சம்.", "Sure, here are a few more."),
    # The one-at-a-time suggestion, the way a salesperson hands over one
    # phone, not a shelf: "this suits you - right, or shall I show another?"
    "say_suggest": (
        "உங்க தேவைக்கு இது நல்லா பொருந்தும்னு நினைக்கிறேன். இது சரியா, வேற பாக்கட்டுமா?",
        "I think this one suits your need well. Is this right, or shall I show another?",
    ),
    "say_list": (
        "சரி, லிஸ்ட்டா காட்றேன். பார்த்து ஒண்ணை தொடுங்க.",
        "Sure, here is the list. Have a look and tap one.",
    ),
    # A brand chosen before a budget that holds nothing of it.
    "say_brand_not_in_budget": (
        "அந்த பிராண்ட்ல இந்த பட்ஜெட்டுக்குள்ள இப்போ எதுவும் இல்ல. இருக்கற பிராண்ட்ல ஒண்ணு சொல்லுங்க.",
        "That brand has nothing inside this budget right now. Please pick from the brands that do.",
    ),
    "say_no_more": (
        "ஸ்டாக்ல இருக்கறது அவ்வளவு தான். கீழ இருக்கறதுல தேர்ந்தெடுங்க.",
        "That is everything in stock. Please choose from the list.",
    ),
    # A brand or model named out loud that the shelf does not hold today.
    "say_not_that": (
        "அது இப்போ ஸ்டாக்ல இல்ல. ஸ்டாக்ல இருக்கறத காட்டியிருக்கேன், இதுல சொல்லுங்க.",
        "That one is not in stock right now. Here is what we have - please pick from these.",
    ),
    "say_dealer": (
        "சரி! ஸ்டாக் இருக்கற பக்கத்து கடைல நம்ம ஆள்கிட்ட பேசலாம். கீழ இருக்கற கடைகள்ல கேளுங்க.",
        "Sure! You can talk to our person at a shop holding stock. Ask at any branch below.",
    ),
    "say_card": ("இதோ முழு விவரம்.", "Here are the full details."),
    "say_down": (
        "மன்னிக்கணும், இப்போ பாக்க முடியல. கொஞ்ச நேரம் கழிச்சு முயற்சி பண்ணுங்க.",
        "Sorry, I cannot check right now. Please try again in a while.",
    ),
    "say_expired": (
        "நேரம் ஆயிடுச்சு. முதல்ல இருந்து ஆரம்பிக்கலாம்.",
        "That took a while. Let us start from the beginning.",
    ),
    "say_slow_down": (
        "கொஞ்சம் பொறுங்க, ஒரு நிமிஷம் கழிச்சு முயற்சி பண்ணுங்க.",
        "Please wait a minute and try again.",
    ),
    # The two ways a customer's own question ends without an answer. "I am
    # not sure" is said when we looked and the facts do not say; "ask at
    # the shop" when we did not look, because the session has had its share.
    "say_unsure": (
        "அது எனக்கு உறுதியா தெரியல. கடைல கேட்டுக்கோங்க.",
        "I am not sure about that. Please ask at the shop.",
    ),
    # Speech that matched no button, and speech the session has not earned.
    "say_again": (
        "மன்னிக்கணும், புரியல. மறுபடி சொல்லுங்க, இல்லன்னா தொடுங்க.",
        "Sorry, I did not catch that. Please say it again, or tap.",
    ),
    "say_tap": (
        "இப்போ தொட்டு தேர்ந்தெடுங்க.",
        "Please tap your choice for now.",
    ),
    "say_enough": (
        "இன்னும் விவரத்துக்கு பக்கத்து பூர்விகா கடைல கேளுங்க.",
        "For anything more, please ask at your nearest Poorvika shop.",
    ),
}


def line(key: str, lang: str = TAMIL) -> str:
    return word(LINES[key], lang)


# On buttons and cards, not spoken.
ANY_BUDGET: Words = ("பட்ஜெட் பரவாயில்ல", "Any budget is fine")
ANY_BRAND: Words = ("எந்த பிராண்டும் பரவாயில்ல", "Any brand is fine")
ANY_OK: Words = ("எதுவானாலும் பரவாயில்ல", "Anything is fine")
ACCEPT_SPEC: Words = ("சரி, அப்படியே பாரு", "Yes, look for that")
OVER_BUDGET: Words = (
    "உங்க பட்ஜெட்டை விட கொஞ்சம் அதிகம்",
    "A little over your budget",
)
SEE_CHEAPER: Words = ("கம்மி விலைல காட்டு", "Show me cheaper")
SEE_MORE: Words = ("இன்னும் காட்டு", "Show me more")
SUGGEST_OK: Words = ("இது சரி, இதையே பாரு", "This is right, open it")
# Features a customer may require, by the measure the shelf keeps for them.
FEATURE_WORDS: dict[str, Words] = {
    "usb_c": ("USB-C", "USB-C"),
    "five_g": ("5G", "5G"),
    "amoled": ("AMOLED ஸ்க்ரீன்", "an AMOLED screen"),
}
CLASH_OTHER: Words = ("வேற பிராண்ட், இதே பட்ஜெட்", "Another brand, same budget")
CONFIRM_YES: Words = ("சரி, அதே தான்", "Yes, that is right")
CONFIRM_CHANGE: Words = ("மாத்தணும்", "Change something")
SUGGEST_NEXT: Words = ("வேற ஒண்ணு காட்டு", "Show me another")
SUGGEST_LIST: Words = ("லிஸ்ட்டா காட்டு", "Show me the list")
BUDGET_UP_YES: Words = ("கொஞ்சம் மேல பரவாயில்ல", "A little above is fine")
BUDGET_UP_NO: Words = ("பட்ஜெட்டுக்குள்ளயே போதும்", "Stay within my budget")
TALK_TO_DEALER: Words = ("கடை ஆள்கிட்ட பேசணும்", "Talk to your person")
INVITE_QUESTION: Words = ("இதைப் பத்தி ஏதாவது கேக்கணுமா?", "Want to ask anything about it?")
SPEAK_BUTTON: Words = ("பேசுங்க", "Speak")
LISTENING: Words = ("கேக்கறேன்…", "Listening…")
HEARD_YOU: Words = ("நீங்க சொன்னது", "You said")

# What a customer may ask about the product they picked. Buttons, not a box
# to type into: every input in the guided call is a choice on the screen.
# That also makes the list closed, so the first customer to ask "what is
# the warranty?" about a product pays for the answer and everyone after
# them does not - and nobody can make this spend by typing.
SUGGESTED: tuple[tuple[str, Words], ...] = (
    ("why", ("இதையே ஏன் சொல்றீங்க?", "Why this one?")),
    ("offers", ("ஆஃபர் இருக்கா?", "Any offers?")),
    ("price", ("இப்போ விலை என்ன?", "Current price?")),
    ("emi", ("EMI எவ்வளவு?", "What is the EMI?")),
    ("where", ("எந்த கடைல கிடைக்கும்?", "Which shops have it?")),
    ("warranty", ("வாரண்டி எவ்வளவு?", "What is the warranty?")),
    ("colours", ("வேற கலர் இருக்கா?", "Any other colours?")),
    ("box", ("பாக்ஸ்ல என்ன வரும்?", "What comes in the box?")),
)

# Spoken lines only: what `make guided-voice` records, one WAV per key.
# English lines go under `<key>_en`. A sentence missing here is shown and
# never spoken, which is the safe way round.
SPOKEN: dict[str, str] = {}
for _key, (_ta, _en) in LINES.items():
    SPOKEN[_key] = _ta
    if _en:
        SPOKEN[f"{_key}_en"] = _en

BRAND_FACET = Facet(BRAND, "ask_brand", ANY_BRAND)
_SIZE = Facet("size", "ask_size", ANY_OK)
_CAPACITY = Facet("capacity", "ask_capacity", ANY_OK)
_KIND = Facet("kind", "ask_kind", ANY_OK)

# What a phone buyer chooses between. The ids are what the recommender is
# told; the labels are what is on the button.
_PHONE_PRIORITIES = (
    Priority(
        "steady",
        ("நடக்கும்போது நிலையான வீடியோ", "Stabilised video while walking"),
        "stabilisation",
    ),
    Priority("camera", ("கேமரா", "Camera"), "camera_mp"),
    Priority("battery", ("பேட்டரி", "Battery"), "battery_mah"),
    Priority("performance", ("வேகம், gaming", "Speed and gaming"), "ram_gb"),
    Priority("storage", ("ஸ்டோரேஜ்", "Storage"), "storage_gb"),
    Priority("value", ("விலைக்கு ஏத்த மதிப்பு", "Value for money")),
)

# What a phone is for, the way customers answer it, and what each answer
# implies. A student is asked what they study and a worker what the work
# is, because that is the question a salesperson actually asks next.
_PHONE_USES = (
    Use(
        "study",
        ("படிப்புக்கு", "For studies"),
        ask_next="ask_student",
        then=(
            Use(
                "school",
                ("பள்ளி, +2 வரை", "School, up to 12th"),
                "battery",
                (
                    "நல்ல பேட்டரி, கொடுத்த காசுக்கு உறுதியான போன்",
                    "Good battery and a sturdy phone",
                ),
            ),
            Use(
                "college",
                ("காலேஜ், டிகிரி", "College, a degree"),
                "value",
                ("விலைக்கு ஏத்த மதிப்பு, நல்ல பேட்டரி", "Value for money with good battery"),
            ),
            # No suggestion: a course name says nothing about the phone
            # (owner's brief, 2026-11-24) - the priority is asked plainly.
            Use(
                "course",
                ("இன்ஜினியரிங், மெடிக்கல் மாதிரி", "Engineering, medical and such"),
                "",
            ),
        ),
    ),
    Use(
        "work",
        ("வேலைக்கு", "For work"),
        ask_next="ask_work",
        then=(
            Use(
                "it",
                ("IT, ஆபீஸ்", "IT or office"),
                ask_next="ask_it_role",
                then=(
                    # No suggestion: a job title is not a spec.
                    Use(
                        "dev",
                        ("டெவலப்பர், டெஸ்டிங்", "Developer or testing"),
                        "",
                    ),
                    Use(
                        "meetings",
                        ("மீட்டிங், கால்ஸ், மெயில்", "Meetings, calls and mail"),
                        "performance",
                        ("வேகம், நாள் முழுக்க நிக்கற பேட்டரி", "Speed with all-day battery"),
                    ),
                    Use(
                        "light",
                        ("லைட் யூஸ் தான்", "Light use only"),
                        "value",
                        ("விலைக்கு ஏத்த மதிப்பு", "Value for money"),
                    ),
                ),
            ),
            Use(
                "business",
                ("சொந்த பிசினஸ்", "My own business"),
                "battery",
                ("நாள் முழுக்க நிக்கற பேட்டரி, ரெண்டு SIM", "All-day battery and dual SIM"),
            ),
            Use(
                "field",
                ("வெளியூர், ஃபீல்ட் வேலை", "Field work, travelling"),
                "battery",
                ("பெரிய பேட்டரி, உறுதியான போன்", "A big battery and a tough phone"),
            ),
            Use(
                "site_video",
                ("சைட்ல வீடியோ, போட்டோ", "Photos and video on site"),
                ask_next="ask_movement",
                overview=("சைட்ல வீடியோ எடுக்க", "to shoot video on site"),
                then=(
                    Use(
                        "moving",
                        ("நடந்துகிட்டே", "While walking"),
                        "steady",
                        (
                            "வீடியோ நிலைத்தன்மை (OIS/EIS) - spec-ல உறுதியா இருக்கறது மட்டும்",
                            "Video stabilisation (OIS/EIS) - only where the spec sheet says so",
                        ),
                    ),
                    Use(
                        "still",
                        ("ஒரே இடத்துல", "From one spot"),
                        "camera",
                        ("நல்ல கேமரா", "A good camera"),
                    ),
                ),
            ),
        ),
        overview=("வேலைக்கு", "for work"),
    ),
    Use(
        "fun",
        ("பொழுதுபோக்குக்கு", "For entertainment"),
        ask_next="ask_fun",
        then=(
            Use(
                "gaming",
                ("கேம்ஸ்", "Games"),
                "performance",
                ("வேகமான processor, அதிக RAM", "A fast processor and plenty of RAM"),
            ),
            Use(
                "camera",
                ("போட்டோ, ரீல்ஸ்", "Photos and reels"),
                "camera",
                ("நல்ல கேமரா முதல்ல", "The camera first"),
            ),
            Use(
                "movies",
                ("சினிமா, OTT", "Movies and OTT"),
                "battery",
                ("பெரிய பேட்டரி, பெரிய ஸ்க்ரீன்", "A big battery and a big screen"),
            ),
        ),
    ),
    Use(
        "family",
        ("வீட்டு பயன்பாடு", "Everyday family use"),
        "value",
        ("விலைக்கு ஏத்த மதிப்பு, நல்ல பேட்டரி", "Value for money with good battery"),
    ),
)

_BRAND_ONLY = (BRAND_FACET,)

_SPEAKER_PRIORITIES = (
    Priority("hdmi", ("டிவிக்கு HDMI ARC", "HDMI ARC for my TV"), "hdmi_arc"),
    Priority("bluetooth", ("ப்ளூடூத் இணைப்பு", "Bluetooth connection"), "bluetooth"),
    Priority(
        "portable", ("பேட்டரியில் இயங்கணும்", "Battery-powered portability"), "portable"
    ),
    _PHONE_PRIORITIES[-1],
)
_VALUE_ONLY = _PHONE_PRIORITIES[-1:]  # "value for money" - every shelf has it

# What a speaker, a TV or a headset is FOR - "ஸ்பீக்கர்னா எந்த ரூமுக்கு?"
# is the owner's own example of the question a salesperson asks next.
_SPEAKER_USES = (
    Use(
        "room",
        ("வீட்டு ரூமுக்கு", "For a room at home"),
        "value",
        ("ரூம் சைஸுக்கு ஏத்த நல்ல சவுண்ட்", "Good sound sized for a room"),
    ),
    Use(
        "hall",
        ("ஹால், டிவியோட சேக்க", "For the hall, with the TV"),
        "value",
        ("பெரிய சவுண்ட், டிவியோட இணைய", "Big sound that pairs with the TV"),
    ),
    Use(
        "party",
        ("பார்ட்டி, வெளில எடுத்துட்டு போக", "Parties and carrying out"),
        "value",
        ("பேட்டரில ஓடற, தூக்கிட்டு போற சவுண்ட்", "Battery-powered, portable sound"),
    ),
)
_TV_USES = (
    Use(
        "hall",
        ("ஹாலுக்கு", "For the hall"),
        "value",
        ("பெரிய ஸ்க்ரீன், நல்ல படம்", "A big screen with a good picture"),
    ),
    Use(
        "bedroom",
        ("பெட்ரூமுக்கு", "For the bedroom"),
        "value",
        ("சின்ன சைஸ், விலைக்கு ஏத்தது", "A smaller size at the right price"),
    ),
)
_HEAD_USES = (
    Use(
        "calls",
        ("கால்ஸ், வேலை", "Calls and work"),
        "value",
        ("தெளிவான மைக், நீண்ட நேரம் அணிய", "A clear mic for long hours"),
    ),
    Use(
        "music",
        ("பாட்டு, சினிமா", "Music and movies"),
        "value",
        ("நல்ல பேஸ், நல்ல சவுண்ட்", "Good bass and sound"),
    ),
    Use(
        "gym",
        ("ஜிம், ஓட்டம்", "Gym and running"),
        "value",
        ("வியர்வை தாங்கற, உறுதியான பிடிப்பு", "Sweat-proof with a firm fit"),
    ),
)

# In the order the first screen shows them: what people come in for most
# comes first. Every shelf here is one the catalogue can FILTER by - see
# `agent.tools.catalogue.CATEGORIES` - because a shelf that cannot be
# filtered cannot be listed whole, and a guided call is a list being narrowed.
SCRIPTS: tuple[Script, ...] = (
    Script(
        "phone",
        ("போன்", "Phone"),
        (10_000, 15_000, 20_000, 30_000, 50_000),
        _BRAND_ONLY,
        _PHONE_PRIORITIES,
        _PHONE_USES,
    ),
    Script(
        "tv",
        ("டிவி", "TV"),
        (20_000, 30_000, 40_000, 60_000, 100_000),
        (_SIZE, BRAND_FACET),
        _VALUE_ONLY,
        _TV_USES,
        "ask_tv_room",
    ),
    Script(
        "laptop", ("லேப்டாப்", "Laptop"), (40_000, 60_000, 80_000, 120_000), _BRAND_ONLY
    ),
    Script(
        "ac", ("ஏசி", "AC"), (30_000, 40_000, 50_000, 65_000), (_CAPACITY, BRAND_FACET)
    ),
    Script(
        "fridge",
        ("ஃப்ரிட்ஜ்", "Fridge"),
        (15_000, 25_000, 40_000, 60_000),
        (_KIND, BRAND_FACET),
    ),
    Script(
        "washing machine",
        ("வாஷிங் மெஷின்", "Washing machine"),
        (15_000, 20_000, 30_000, 45_000),
        (_KIND, BRAND_FACET),
    ),
    Script(
        "tablet", ("டேப்லெட்", "Tablet"), (10_000, 20_000, 40_000, 70_000), _BRAND_ONLY
    ),
    Script(
        "watch",
        ("ஸ்மார்ட் வாட்ச்", "Smart watch"),
        (2_000, 5_000, 10_000, 25_000),
        _BRAND_ONLY,
    ),
    Script(
        "headphones",
        ("ஹெட்ஃபோன்", "Headphones"),
        (1_000, 2_500, 5_000, 15_000),
        (_KIND, BRAND_FACET),
        _VALUE_ONLY,
        _HEAD_USES,
        "ask_head_for",
    ),
    Script(
        "speaker",
        ("ஸ்பீக்கர்", "Speaker"),
        (2_000, 5_000, 10_000, 25_000),
        _BRAND_ONLY,
        _SPEAKER_PRIORITIES,
        _SPEAKER_USES,
        "ask_speaker_for",
    ),
    Script("fan", ("ஃபேன்", "Fan"), (2_000, 3_500, 5_000, 8_000), _BRAND_ONLY),
    # The smaller shelves. Found by counting what the index calls sellable:
    # 207 of its 463 in-stock records sat outside the eleven shelves above,
    # and these six reach 115 of them. The rest live under facet values the
    # catalogue cannot filter by (screen protectors, cables, memory cards).
    Script("powerbank", ("பவர் பேங்க்", "Power bank"), (1_000, 2_000, 3_500), _BRAND_ONLY),
    Script("charger", ("சார்ஜர்", "Charger"), (500, 1_000, 2_000), _BRAND_ONLY),
    Script("case", ("கேஸ், கவர்", "Cases and covers"), (300, 700, 1_500), _BRAND_ONLY),
    Script("geyser", ("கீசர்", "Geyser"), (3_000, 6_000, 10_000), _BRAND_ONLY),
    Script(
        "kitchen",
        ("கிச்சன் பொருட்கள்", "Kitchen appliances"),
        (1_000, 3_000, 6_000, 12_000),
        _BRAND_ONLY,
    ),
    Script(
        "home appliance",
        ("வீட்டு பொருட்கள்", "Home appliances"),
        (1_000, 3_000, 8_000, 20_000),
        _BRAND_ONLY,
    ),
)

BY_SHELF: dict[str, Script] = {script.shelf: script for script in SCRIPTS}

LANGUAGE_CHOICES: tuple[Choice, ...] = (
    Choice(TAMIL, "தமிழ்"),
    Choice(ENGLISH, "English"),
)


def shelf_choices(lang: str) -> tuple[Choice, ...]:
    return tuple(Choice(script.shelf, word(script.words, lang)) for script in SCRIPTS)


def rupees(amount: int) -> str:
    """₹1,25,000 - grouped the way a price tag in Chennai groups it.

    Python's own `,` groups in thousands, which prints a lakh as 100,000 and
    reads as a typing mistake to anyone who shops in rupees.
    """
    digits = str(abs(int(amount)))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        pairs: list[str] = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *pairs, tail])
    return ("-" if amount < 0 else "") + "₹" + digits
