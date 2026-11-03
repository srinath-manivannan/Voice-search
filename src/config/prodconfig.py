"""Production defaults.

Identical to development apart from log level. Any divergence that appears here
later should carry a comment saying why, because a setting that differs between
environments is a bug that only shows up in one of them.
"""

GEMINI_LIVE_MODEL = "gemini-3.8-live"
REPLY_WORD_CAP = 15
THINKING_BUDGET = 0
LOG_LEVEL = "INFO"
