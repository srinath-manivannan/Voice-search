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

# Context window compression, and the floor underneath it.
#
# The trigger CANNOT be sized from the cost model alone. 400/100 was, and it
# broke every call that used a tool: the standing instruction and the tool
# declarations are about 1,450 tokens on their own and compression always
# keeps them, so a 400-token trigger fires on every single turn and reduces
# the window to the instruction. The function call then no longer precedes
# its response and the Live API closes the socket - 1007, "Request contains
# an invalid argument" - which reaches a caller as a call that simply stops.
#
# 2,000/1,500 survives the tool call and is still wrong: measured over four
# turns the agent answered "Endha model pathi kekureenga?" to every one of
# them, because the window had been emptied of what it just said. A window
# that cannot hold the conversation does not cost less, it costs the call.
#
# So the trigger has a floor, and these numbers are where the cliff actually
# is rather than where arithmetic put it. Probed on a real endpoint, same
# four turns each time:
#
#     400/100     the call dies on the first tool call
#   2,000/1,500   survives, remembers nothing
#   2,500/2,000   survives, remembers nothing
#   3,000/2,400   remembers; held over eight turns and two compressions
#   4,000/3,000   remembers, and costs about Rs 1.2 a minute more
#
# 3,000/2,400 is shipped: one step above a measured failure, not two, and the
# assertions in tests/test_config.py sit on that boundary deliberately -
# 2,500/2,000 fails them, 3,000/2,400 passes.
#
# One thing it does NOT buy back: at turn eight the agent had lost the budget
# the caller opened with, because compression drops the oldest turn and that
# is where the budget was said. A budget belongs in ToolSession beside the
# shortlist, not in audio history. Until it is, this is a known limit.
#
# What this means for cost is in docs/COST.md, and it is not what that file
# said before: compression is a brake on long calls, not the big lever. The
# levers that survive are the size of this prompt and the number of
# generations a turn takes. Trigger 0 switches compression off entirely.
#
# One more thing, found after the probes and worth more than they are:
# Google's Cloud documentation gives a MINIMUM for this field. "Maximum
# context length (trigger_tokens): minimum value 5,000, maximum 128,000."
# 3,000 is below it. It held across eight turns and two compressions here,
# but so did 400 for exactly one turn, and nothing documents what the API
# does with an out-of-range value - it is not rejected at setup.
#
# That minimum has a consequence bigger than this setting. A three-minute
# call accumulates about 6,500 tokens in total, so a trigger at the
# documented floor of 5,000 barely fires before the caller hangs up.
# In-spec compression is built for long sessions, not for making a short
# call cheaper. If another 1007 ever appears, raise this to 5,000 first -
# and expect the bill to go up, not down.
CONTEXT_TRIGGER_TOKENS = 3000
CONTEXT_TARGET_TOKENS = 2400

# Browsers allowed to open a socket here. Development is the machine you are
# sitting at; production names the real host and nothing else.
ALLOWED_ORIGINS = "http://127.0.0.1:8080,http://localhost:8080"

# Session rotation. 600 audio tokens is about twenty-four seconds of retained
# speech, which measured as roughly two exchanges - the point at which
# re-billing the history costs more than carrying a summary. Zero switches it
# off and leaves one socket for the whole call.
ROTATE_ABOVE_AUDIO_TOKENS = 600
