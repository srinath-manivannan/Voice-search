"""English the shopper sees, in one place.

The widget paints the welcome itself, with no network. The server sends the
same sentences for everything that happens after a message is sent, so a
failure state cannot drift into a fragment or a second language.
"""

from __future__ import annotations

WELCOME = (
    "Hi! I'm the Shopping Assistant. Ask me about any product — prices, "
    "stock near you, offers or EMI. You can type in English, Tamil or Tanglish."
)

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
    "Best phone under ₹25,000",
    "Compare similar",
    "EMI options",
)
