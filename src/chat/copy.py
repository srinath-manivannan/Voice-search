"""English the shopper sees, in one place.

The widget paints the welcome itself, with no network. The server sends the
same sentences for everything that happens after a message is sent, so a
failure state cannot drift into a fragment or a second language.
"""

from __future__ import annotations


def welcome_line(name: str = "Shopping Assistant") -> str:
    """The headline painted into the page. No network call is required to show it."""
    return f"Hi! I'm {name}. What can I help you with?"


WELCOME = welcome_line()

SUBTITLE = (
    "Your smart shopping guide — products, offers, stores & support. "
    "Ask me anything, anytime!"
)

PLACEHOLDER = 'Try: "Best phone under ₹30,000 with good camera"'

LIMIT_REACHED = (
    "I can't answer more questions in this chat right now. You can keep "
    "browsing, and product pages show live prices."
)

UNAVAILABLE = "The assistant is unavailable right now."

SERVICE_ERROR = (
    "Sorry, something went wrong on our side. You weren't charged. [Try again]"
)

SEARCH_DOWN = "I can't search products right now. You can keep browsing. [Try again]"

STOCK_UNCONFIRMED = (
    "I couldn't confirm stock right now. The price above is current. [Check again]"
)

PRICE_UNCONFIRMED = "Price couldn't be confirmed, see the product page."

OFFER_UNCONFIRMED = "Offer and EMI details couldn't be confirmed."

RETRY = "Message not sent. [Retry]"

OFFLINE = "You're offline. We'll send your message when you're back online."

RECONNECTING = "Reconnecting…"

CHECKING = "Checking…"

STILL_CHECKING = "Still checking live prices and stock…"

FOOTER = "AI answers · prices and stock checked live · confirm at checkout"

SUGGESTIONS = (
    "Is this available?",
    "Help me find the right phone",
    "Best AC for my room",
    "Laptop under ₹50,000",
    "Check store availability",
)
