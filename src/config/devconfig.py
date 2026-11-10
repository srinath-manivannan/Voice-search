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

# Context window compression. This is the single biggest cost lever in the
# project, and the first setting of it was useless: Google re-processes the
# WHOLE context window on every turn and bills audio history at the audio
# input rate, so at 4,000/2,000 the window held more than a three-minute call
# ever accumulated and compression saved nothing - the model priced that
# configuration at Rs 9.19 a minute against Rs 9.01 with it switched off.
#
# 400/100 is sized from the cost model, not guessed: 400 tokens is about
# thirteen seconds of audio, so the window holds the exchange in progress and
# little else. That is what gets a chatty caller in under Rs 2.50 a minute
# while leaving the agent's own voice - half the remaining cost - untouched.
#
# It is only safe because the conversation's state does NOT live in the
# window. The shortlist a caller refers back to is held on our side in
# ToolSession, so "the second one" survives a compression that throws away
# the audio of being told about it. Trigger 0 switches compression off.
CONTEXT_TRIGGER_TOKENS = 400
CONTEXT_TARGET_TOKENS = 100
