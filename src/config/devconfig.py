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
