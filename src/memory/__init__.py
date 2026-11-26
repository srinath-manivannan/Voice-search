"""Side memory for a live call.

Gemini Live stays the voice. This package only writes the short text note a
fresh socket is handed, and it does that after the reply has already started,
never on the way to the first audio byte.
"""
