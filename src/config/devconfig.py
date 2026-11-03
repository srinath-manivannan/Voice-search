"""Development defaults.

Thinking is off here for the same reason it is off in production - Phase 0
measured it at 126 tokens per turn, billed, and invisible in totalTokenCount.
Keeping dev and prod the same on this means a latency or quality regression
shows up locally rather than on a customer call.
"""

GEMINI_LIVE_MODEL = "gemini-3.8-live"
REPLY_WORD_CAP = 15
THINKING_BUDGET = 0
LOG_LEVEL = "DEBUG"
