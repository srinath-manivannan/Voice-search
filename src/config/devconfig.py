"""Development defaults.

Thinking is off here for the same reason it is off in production - Phase 0
measured it at 126 tokens per turn, billed, and invisible in totalTokenCount.
Keeping dev and prod the same on this means a latency or quality regression
shows up locally rather than on a customer call.
"""

GEMINI_LIVE_MODEL = "gemini-3.8-live"
GEMINI_TEXT_MODEL = "gemini-3.5-flash-lite"

# Product List API. Staging for now - production needs its own URL
# and token, and neither has been confirmed.
CATALOGUE_URL = "https://stage-proxy.poorvika.in/mMBVOUVdxJWP"
REPLY_WORD_CAP = 15
CHAT_WORD_CAP = 45
THINKING_BUDGET = 0
LOG_LEVEL = "DEBUG"

# The per-item estate: PIM pricing, Central Inventory, the offers rule engine
# and the EMI calculator the shop's own product page calls. Measured on
# 2026-11-04: production answers price, offers and EMI and has no working
# stock credential; stage answers stock and has no EMI host at all. One
# environment is chosen whole, because production pricing spoken beside
# staging stock is the failure worth designing against.
ESTATE_API_ENV = "production"
# "inprocess" runs the Worker's own product service here; "worker" calls
# POST /product on a deployed one; "off" disables per-item facts entirely.
ESTATE_BACKEND = "inprocess"
# Relative to this repository's root, not the working directory.
ESTATE_SRC_PATH = "../../Search-proxy/products-search-engine"

# Context window compression. Audio is 25 tokens a second, so a three-minute
# call accumulates roughly 4,500 tokens of itself on top of the instruction -
# and a trigger of 8,000 would barely fire before a real call has ended.
# 4,000 starts compressing around ninety seconds in, which is where a call
# begins carrying the weight of itself. A sliding window keeps the recent
# exchanges, and the shortlist a caller refers back to is held on our side in
# ToolSession, so compression cannot take it away. Trigger 0 switches it off.
CONTEXT_TRIGGER_TOKENS = 4000
CONTEXT_TARGET_TOKENS = 2000
