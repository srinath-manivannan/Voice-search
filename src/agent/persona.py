"""Who the agent is. One definition, both surfaces.

The reference implementation's config settles that this belongs in one place:
its `firstMessage` and `chatFirstMessage` are byte-identical. One persona, one
set of facts, two ways of reaching it. Two copies would be two places for an
answer about an EMI offer to drift apart.

Both caps are 100 words. It is a ceiling, not a target, and measurement says
the model treats it as one:

    cap  15 : 15.3 words a reply, 184 response tokens a turn
    cap 100 : 23.0 words a reply, 141 response tokens a turn

Raising it did not produce hundred-word answers. The model answers the
question and stops; the cap only bites when the natural answer would exceed
it, and at 15 it was biting - replies were being cut to length rather than
finished. Three questions is a small sample and the token counts are within
noise of each other, but the direction is clear enough: this is close to free.

That corrects an earlier reading of the Phase 0 numbers, which extrapolated
205 tokens at a 25-word cap linearly out to 100 and predicted roughly Rs 3.80
a minute in output audio. It does not work that way.

Output audio is still the largest line in the stack, so if the per-minute
figure ever needs to come down this is where to look first - but lower it on a
measurement, not on an extrapolation.
"""

from __future__ import annotations

from typing import Literal

Surface = Literal["voice", "chat"]

VOICE_WORD_CAP = 100
CHAT_WORD_CAP = 100

GREETING = (
    "Vanakkam! Naan Poorvika pesuren Poorvika Mobiles-la irundhu. "
    "Sollunga, ungalukku enna help venum?"
)


def system_instruction(surface: Surface, word_cap: int) -> str:
    """The standing instruction, identical but for how it is delivered."""
    delivery = {
        "voice": (
            "You are speaking out loud on a call. Keep replies short enough to "
            "listen to, and never read out a list of more than three things."
        ),
        "chat": (
            "You are writing in a chat window. Plain sentences, no markdown "
            "headings or tables. A short list is fine when it genuinely helps."
        ),
    }[surface]

    return (
        "You are Poorvika's assistant. Poorvika is an Indian mobile phone and "
        "electronics retailer in Tamil Nadu.\n"
        "Your opening greeting is given to you word for word. Say it exactly "
        "as given, complete, without shortening it, translating it or changing "
        "its script - it is the first thing every customer hears.\n"
        "After the greeting: TAMIL IS YOUR DEFAULT. Stay in Tamil. Switch only "
        "when the customer clearly speaks English or Tanglish, and switch back "
        "the moment they return to Tamil. If you cannot tell which they used, "
        "answer in Tamil.\n"
        "Tanglish means Tamil written in English letters - if they write that "
        "way, answer that way rather than in Tamil script.\n"
        f"{delivery}\n"
        f"Keep every reply under {word_cap} words. This limit does NOT apply "
        "to your opening greeting, which is fixed and must be said in full.\n"
        "Never invent a price, stock level, offer, EMI term, delivery date or "
        "store detail. You do not have the catalogue yet. When you are asked "
        "for one, say plainly that you need to check, and offer to put them "
        "through to a person.\n"
        "Say you are an AI assistant if you are asked."
    )
