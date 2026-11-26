"""Development defaults.

Thinking is off here for the same reason it is off in production - Phase 0
measured it at 126 tokens per turn, billed, and invisible in totalTokenCount.
Keeping dev and prod the same on this means a latency or quality regression
shows up locally rather than on a customer call.
"""

GEMINI_LIVE_MODEL = "gemini-3.8-live"

# Which prebuilt voice the agent speaks in. Empty means the API's own
# default, which is where this started and is a liability: src/agent/
# greeting.pcm is a recording of this voice, and a default that moves makes
# the opening line sound like a different person from the rest of the call.
# Name it here and the two cannot drift. Re-run `make greeting` after
# changing it - the recording is of the OLD voice until you do.
VOICE_NAME = ""
GEMINI_TEXT_MODEL = "gemini-3.5-flash-lite"

# Product List API. Staging for now - production needs its own URL
# and token, and neither has been confirmed.
CATALOGUE_URL = "https://stage-proxy.poorvika.in/mMBVOUVdxJWP"
# 18, chosen 2026-11-15 after 25 was tried and measured.
#
# The reply docs/CASCADE.md asks for - two products, a reason each, one
# question - is about 18 words at its shortest, and the persona's own worked
# example had been over the old cap of 15 since long before it was noticed.
#
# 25 was the first answer and it cost too much. Output audio is charged once
# and nothing discounts it, and the model writes TO the cap rather than under
# it: replies measured 5.1 s at 15, modelled 8.5 s at 25.
#
#     cap   s/reply   Rs/min   vs Rs 2.50
#      15       5.1     2.58        +0.08
#      18       6.1     2.82        +0.32   <- here
#      25       8.5     3.37        +0.87
#
# A fuller answer does avoid follow-up turns and an avoided turn pays for
# 5.6 s of extra speech, but 25 added 25 s a minute - it would have had to
# avoid 4.5 turns out of 7.4 to pay for itself. 18 buys the room the reply
# actually needs and leaves the rest.
REPLY_WORD_CAP = 18
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
# The estate code shipped with this repo (make vendor-estate refreshes it);
# point ESTATE_SRC_PATH at a Search-proxy checkout to develop against it.
ESTATE_SRC_PATH = "vendor/estate"

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
# Raised from 3000/2400 when the prompt grew.
#
# The target has to leave room for a few exchanges AFTER the prompt is kept,
# or compression hands the model a window with nothing in it but its own
# instructions - the failure this guard was written for, where the agent
# asked "Endha model pathi kekureenga?" four turns running because every
# turn emptied the window of what it had just said.
#
# Opening the shelves up and teaching it to narrow cost about 240 prompt
# tokens, and at 2,400 that left 931 for the conversation. The 400 here buys
# the room back. It is not free: once the trigger fires, up to 400 more
# tokens are carried and re-billed on each turn after it. Rotation at 400
# audio tokens is what actually keeps this call small, and compression
# almost never fires behind it - this is the floor under that, and a floor
# that breaks the conversation is not one.
CONTEXT_TRIGGER_TOKENS = 3400
CONTEXT_TARGET_TOKENS = 2800

# Browsers allowed to open a socket here. Development is the machine you are
# sitting at; production names the real host and nothing else.
ALLOWED_ORIGINS = "http://127.0.0.1:8080,http://localhost:8080"

# Session rotation. A measured call accumulated 195 audio tokens a generation
# - the caller's speech and the agent's both stay in the window - so 400 is a
# rotation about every two generations, and 600 was about every three.
#
# 400 rather than lower because the spare socket takes 1,735 ms to open and is
# opened while the agent is still speaking: at two generations there is a whole
# reply to hide it in, and at 200 - one rotation per generation - there is not.
# Worth Rs 0.25 a connected minute against 600. Zero switches rotation off and
# leaves one socket for the whole call. See docs/COST.md.
#
# 300 since 2026-11-25, measured with src/tools/measure_live (same two Tamil
# lines every run): Rs 2.97 a minute at 400, Rs 2.39 at 300 - together with
# the handover note now carrying refs and stock, without which the fresh
# socket re-ran a search to find "the first one". The spare is still hidden:
# it opens on the tool-call generation and the EMI answer took about three
# seconds at both settings.
ROTATE_ABOVE_AUDIO_TOKENS = 300
