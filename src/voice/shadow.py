"""A second ear on the call, listening only so the first can be measured.

[docs/CASCADE.md](../../docs/CASCADE.md) proposes moving the conversation off
native audio: Whisper for the ear, flash-lite on text for the brain, the Live
session demoted to a mouth. It is worth Rs 1.27 a connected minute and it
costs about 1.3 seconds on every turn a customer waits through.

That trade cannot be made on a rate card. It rests on one question nobody has
measured - **does Whisper hear a Tanglish caller as well as the Live API
does?** - and the cheapest honest way to find out is to send both the same
frames on real calls and read the two transcripts side by side.

So this is step one of six, and it is deliberately the one that changes
nothing. It forwards no audio, answers no caller, and holds up no reply. The
call cannot tell it is here.

## What is actually compared

Two numbers per utterance, and they measure different risks.

**Agreement** is a token-level similarity, and it is the soft one. Tanglish
romanises inconsistently - "venum", "vendum", "vEndum" - so two transcripts
can disagree on half their characters and mean exactly the same thing. A
middling agreement score is not a failure; it is Tanglish.

**The digits are the hard one.** `src/agent/prefetch.py` reads a budget out
of the caller's words with a regular expression over digit runs, and
`search_products` is called with those numbers. If Whisper writes a budget
the way Tamil says it rather than the way a keypad does, the prefetch will
not fire on its output and the search will be wrong - and that is a blocker
for step 3 rather than a rough edge. So the digit sets are compared exactly,
and a mismatch is reported as a mismatch even when the two transcripts
obviously agree about what was said.

## What it never does

It never fails the call. A Groq outage, a timeout, an empty transcript or a
key that was never set leaves the conversation exactly as it was, because
nothing on the path to the caller's ear awaits anything in this file.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.voice.whisper import Heard, seconds_of

log = logging.getLogger(__name__)

# One utterance of a phone call. Past this the caller is monologuing and the
# ten-second floor has stopped mattering anyway; the buffer is capped so a
# socket that never reports a turn boundary cannot grow without limit.
MAX_UTTERANCE_SECONDS = 60.0

# Utterances waiting for their Live transcript. A barge-in can start a second
# utterance before the first one's transcript arrives, so this is not always
# one - but it is never many, and dropping the oldest is better than holding
# a call's worth of audio to compare something nobody will read.
MAX_PENDING = 4

_DIGITS = re.compile(r"\d[\d,]*")

# Unicode categories that are punctuation or a symbol, and nothing else.
# `[^\w\s]` would have been shorter and wrong in the same way `\b` was wrong
# in prefetch.py: the pulli and the vowel signs that make வேணும் a word are
# nonspacing marks, category Mn, which `\w` does not match - so that pattern
# cut the word into வ, ண, ம and compared three letters against one word.
_STRIPPED = {"P", "S"}


class Transcriber(Protocol):
    """The slice of `GroqWhisper` this file needs. A test stands in."""

    async def hear(self, pcm: bytes, *, rate: int) -> Heard: ...


class Recorder(Protocol):
    """The slice of `CallTrace` this file needs."""

    def record(self, kind: str, **detail: Any) -> Any: ...


def words(text: str) -> list[str]:
    """Lowercased words, punctuation gone. Enough to compare two spellings.

    Splitting on category rather than on `\\w` is what keeps a Tamil word
    whole; see `_STRIPPED`.
    """
    return "".join(
        " " if unicodedata.category(char)[0] in _STRIPPED else char
        for char in text.lower()
    ).split()


def agreement(live: str, shadow: str) -> float:
    """How alike two transcripts are, 0.0 to 1.0, on words not characters.

    Characters would punish romanisation differences twice: once for the
    spelling and again for the length. Two transcripts that chose "venum" and
    "vendum" disagree about one word, and that is what this says.
    """
    left, right = words(live), words(shadow)
    if not left and not right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def digits(text: str) -> set[str]:
    """Every number written as digits, commas removed.

    A set rather than a list: "20000 to 25000 kulla 20000" is the same budget
    said twice, and ordering is not what the prefetch reads.
    """
    return {match.replace(",", "") for match in _DIGITS.findall(text)}


@dataclass
class _Pending:
    """An utterance that has been sent to Groq and not yet paired."""

    index: int
    seconds: float
    task: asyncio.Task[Heard | None]
    live: str | None = None


@dataclass
class Tally:
    """What the whole call showed, for one line in the trace at the end."""

    utterances: int = 0
    compared: int = 0
    failed: int = 0
    dropped: int = 0
    billed_seconds: float = 0.0
    usd: float = 0.0
    agreements: list[float] = field(default_factory=list)
    digits_matched: int = 0

    @property
    def mean_agreement(self) -> float:
        return sum(self.agreements) / len(self.agreements) if self.agreements else 0.0

    def as_detail(self) -> dict[str, Any]:
        return {
            "utterances": self.utterances,
            "compared": self.compared,
            "failed": self.failed,
            "dropped": self.dropped,
            "mean_agreement": round(self.mean_agreement, 3),
            "digits_matched": self.digits_matched,
            "billed_seconds": round(self.billed_seconds, 1),
            "usd": round(self.usd, 5),
        }


class ShadowEar:
    """The same frames, a second transcriber, and two numbers per utterance.

    Driven from `Conversation`: `hears` on every frame that is forwarded to
    the model, `caller_stopped` when the turn ends, `live_said` when the Live
    API's own transcript for that turn arrives.
    """

    def __init__(
        self,
        whisper: Transcriber,
        trace: Recorder,
        *,
        rate: int,
        timeout_s: float = 8.0,
    ) -> None:
        self._whisper = whisper
        self._trace = trace
        self._rate = rate
        self._timeout = timeout_s
        self._buffer = bytearray()
        self._cap = int(MAX_UTTERANCE_SECONDS * rate * 2)
        self._pending: deque[_Pending] = deque()
        self._index = 0
        self.tally = Tally()

    # -- driven from the microphone pump, so neither of these may await ----

    def hears(self, frame: bytes) -> None:
        """One frame of caller audio, as forwarded to the model.

        Called once per frame from the pump, so it does nothing but append.
        Over the cap it stops appending rather than dropping the front: the
        start of an utterance is what carries the budget.
        """
        if len(self._buffer) < self._cap:
            self._buffer += frame

    def caller_stopped(self) -> None:
        """The turn ended. Send what was buffered and start listening again."""
        pcm, self._buffer = bytes(self._buffer), bytearray()
        if not pcm:
            return
        self._index += 1
        self.tally.utterances += 1
        pending = _Pending(
            index=self._index,
            seconds=seconds_of(pcm, self._rate),
            task=asyncio.create_task(self._hear(pcm), name="shadow-ear"),
        )
        self._pending.append(pending)
        while len(self._pending) > MAX_PENDING:
            stale = self._pending.popleft()
            stale.task.cancel()
            self.tally.dropped += 1
            self._trace.record("shadow_dropped", utterance=stale.index)

    @property
    def in_flight(self) -> tuple[asyncio.Task[Heard | None], ...]:
        """Transcriptions that have not come back. Teardown and tests."""
        return tuple(p.task for p in self._pending if not p.task.done())

    # -- driven from the event loop, which may await -----------------------

    def live_said(self, text: str) -> None:
        """The Live API's transcript for the oldest unpaired utterance."""
        for pending in self._pending:
            if pending.live is None:
                pending.live = text
                return

    async def settle(self) -> None:
        """Pair everything that has both halves. Never raises.

        Called after a reply rather than during one, so a slow Groq delays
        nothing a caller is waiting for.
        """
        while self._pending and self._pending[0].task.done():
            await self._compare(self._pending.popleft())

    async def close(self) -> None:
        """End of call: let what is in flight finish, then total it up."""
        for pending in list(self._pending):
            try:
                await asyncio.wait_for(asyncio.shield(pending.task), self._timeout)
            except Exception:  # noqa: BLE001 - a late transcript is not an error
                pass
        while self._pending:
            await self._compare(self._pending.popleft())
        self._trace.record("shadow_ear", **self.tally.as_detail())

    # -- internals ---------------------------------------------------------

    async def _hear(self, pcm: bytes) -> Heard | None:
        try:
            return await asyncio.wait_for(
                self._whisper.hear(pcm, rate=self._rate), self._timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001 - never the caller's problem
            log.info("shadow ear could not transcribe: %s", type(failed).__name__)
            return None

    async def _compare(self, pending: _Pending) -> None:
        # Read off the task rather than awaited. `close` shields and times
        # out, so a task can reach here unfinished, and awaiting a cancelled
        # one raises CancelledError - which would take down the caller of a
        # method whose whole contract is that it cannot.
        heard = None
        if pending.task.done() and not pending.task.cancelled():
            if pending.task.exception() is None:
                heard = pending.task.result()
        if heard is None:
            self.tally.failed += 1
            self._trace.record(
                "shadow_failed",
                utterance=pending.index,
                seconds=round(pending.seconds, 1),
            )
            return

        self.tally.billed_seconds += heard.billed_seconds
        self.tally.usd += heard.usd

        live = pending.live
        if live is None:
            # Groq heard something the Live API never reported. Worth knowing
            # on its own - it is the direction that would make the cascade
            # look better, so it is recorded rather than quietly skipped.
            self._trace.record(
                "shadow_unpaired", utterance=pending.index, shadow=heard.text
            )
            return

        score = agreement(live, heard.text)
        same_digits = digits(live) == digits(heard.text)
        self.tally.compared += 1
        self.tally.agreements.append(score)
        self.tally.digits_matched += int(same_digits)
        self._trace.record(
            "shadow_compared",
            utterance=pending.index,
            seconds=round(pending.seconds, 1),
            agreement=round(score, 3),
            digits_match=same_digits,
            live=live,
            shadow=heard.text,
        )
