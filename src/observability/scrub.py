"""What may leave this process, and what must not.

An error report is the easiest place in a system to leak a secret, because
it is assembled under pressure from whatever happens to be in scope. This
service holds two kinds of thing that must never reach a third party:

**Credentials.** The Gemini key, the catalogue token, the four estate
tokens. Pattern-matching for them is not enough - a token is just a string -
so the actual values are registered at startup and redacted by equality,
which catches them wherever they turn up: in a URL, in a header dict, half
way through a traceback line. The patterns are a second net for the ones
nobody registered.

**The customer.** What a caller said, and what the agent said back. The
page promises nothing is recorded or stored, and an error report that
carries a transcript breaks that promise quietly, in a system nobody is
looking at. Fields that carry speech are dropped entirely rather than
shortened, because half a sentence is still a sentence.

A value this cannot classify is kept. Scrubbing everything would make the
reports useless, which is how a team learns to ignore them.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# A credential rarely arrives whole. It gets truncated into a log line, cut
# by a line wrap, or sliced by somebody's own defensive `[:40]` - and a
# fragment of a key is still a key. So a known secret is matched by any run
# of this many characters, not only in full.
#
# Found by probing the running server rather than by reading this file: the
# whole key did not appear in the log and forty characters of it did.
# Sixteen characters of a random token is specific enough that a false
# positive is not a real risk.
MIN_FRAGMENT = 16
# A guard against pathological cost, not a security limit: the substring
# scan is quadratic in the secret's length and runs on an error path.
MAX_SECRET = 512

# Registered at startup from the live config. Equality beats cleverness: a
# token is a string with no shape, and the one certain way to recognise it
# is to have been told it.
_known: set[str] = set()

# The second net, for whatever was never registered. Google keys have a
# recognisable prefix; the rest are the shapes a credential arrives in.
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
# Run BEFORE the generic one, or "Authorization: Bearer xyz" is read as the
# name "authorization" with the value "Bearer", and the token survives.
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
# The quote after the name is optional because JSON writes `"token": "..."`
# and a header writes `token: ...`, and both turn up in a traceback.
_NAMED = re.compile(
    r"(?i)(authorization|x-goog-api-key|api[-_]?key|token|secret|password)"
    r"[\"']?\s*[:=]\s*[\"']?([^\s\"',}&]+)"
)
# A key in a query string is the classic one. httpx puts the whole URL in
# its exception text, and this project already learned that once.
_IN_URL = re.compile(r"(?i)([?&](?:key|token|access_token)=)[^&\s]+")

# Anything under one of these names is speech or the audio of it. Dropped,
# never shortened: half a sentence is still a sentence.
_SPEECH = frozenset(
    {
        "text",
        "transcript",
        "transcription",
        "speech",
        "said",
        "utterance",
        "pcm",
        "audio",
        "data",
        "inline_data",
        "inlineData",
        "parts",
        "turns",
        "contents",
        "message_text",
    }
)

_SECRETISH = re.compile(r"(?i)(key|token|secret|password|credential|dsn)")


def register(*values: str) -> None:
    """Teach the scrubber a secret, so it can be redacted by equality."""
    for value in values:
        # Short values are not secrets, they are flags - and redacting "0"
        # or "bot" everywhere would make every report unreadable.
        if value and len(value) >= 8:
            _known.add(value)


def forget_all() -> None:
    """Tests only."""
    _known.clear()


def text(value: str) -> str:
    """One string, with everything recognisable taken out."""
    cleaned = value
    for secret in _known:
        cleaned = _without(cleaned, secret)
    cleaned = _GOOGLE_KEY.sub(REDACTED, cleaned)
    cleaned = _BEARER.sub(f"bearer {REDACTED}", cleaned)
    cleaned = _NAMED.sub(lambda m: f"{m.group(1)}={REDACTED}", cleaned)
    cleaned = _IN_URL.sub(lambda m: f"{m.group(1)}{REDACTED}", cleaned)
    return cleaned


def _without(haystack: str, secret: str) -> str:
    """Take out the secret, and any long enough piece of it.

    Longest first, so a near-whole match is replaced once rather than being
    chewed into several separate redactions.
    """
    if secret in haystack:
        return haystack.replace(secret, REDACTED)
    if len(secret) > MAX_SECRET:
        return haystack
    for length in range(len(secret) - 1, MIN_FRAGMENT - 1, -1):
        for start in range(len(secret) - length + 1):
            fragment = secret[start : start + length]
            if fragment in haystack:
                return haystack.replace(fragment, REDACTED)
    return haystack


def data(value: Any, _depth: int = 0) -> Any:
    """A structure, scrubbed key by key.

    Depth-limited: an error context can hold a cyclic or enormous object,
    and a reporter that hangs on one is worse than one that reports less.
    """
    if _depth > 6:
        return "[too deep]"
    if isinstance(value, str):
        return text(value)
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _SPEECH and not isinstance(item, (int, float, bool)):
                # The names overlap with harmless counts - the Live API
                # calls conversation content "turns" and so does our own
                # error context, where it is how many there were. A number
                # is not a sentence, so it stays.
                out[name] = REDACTED
            elif _SECRETISH.search(name):
                out[name] = REDACTED
            else:
                out[name] = data(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [data(item, _depth + 1) for item in value[:20]]
    return value
