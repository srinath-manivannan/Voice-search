"""A compact handover, written after the reply, never before it.

Live keeps its own audio and re-bills it. The way out of that is a fresh
socket carrying a few sentences of text. `handover.note` already builds the
sentences that must be exact — the budget and the shortlist, taken from
what the tools returned. This adds the part the tools do not know: what the
customer is trying to do, which language they are actually speaking, and
what is still unanswered.

It runs every N caller replies, and only once that reply has started coming
back. Grok is not on the path to the first audio byte. If the call fails,
times out, or comes back empty, the previous note stands, and if there has
never been one the rotation carries the tool note alone. A summary is not
allowed to take the call down with it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from src.memory.grok import GrokCompletion, GrokError

log = logging.getLogger(__name__)

# The Live window re-bills every token of this note on every later
# generation. 150 tokens is the budget the cost plan was written to. The
# API is asked for that many, and we cut again on the way in, because a
# model that ignores max_tokens would otherwise hand the next socket an
# essay.
MAX_TOKENS = 150
CHAR_CAP = 600

# What the next socket needs, and nothing it could quote as a price it was
# not given. Prices stay in the tool note, which is written from the
# shortlist and cannot invent one.
SYSTEM = (
    "You write a handover for the next moment of the same phone call at "
    "Poorvika, a phone shop in Tamil Nadu. Use only the transcript and the "
    "previous note. Do not invent a price, a product, a budget, or a "
    "specification. If a number was not said, leave it out.\n"
    "Plain lines, no markdown, nothing after the last line:\n"
    "Budget:\n"
    "Offered:\n"
    "Discussing:\n"
    "Intent:\n"
    "Language: Tamil or Tanglish or unknown\n"
    "Open:\n"
    "Stay under 120 words."
)


class ChatModel(Protocol):
    """The slice of Grok this file needs. A test stands in with a fake."""

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> GrokCompletion: ...

    @property
    def model(self) -> str: ...


UsageSink = Callable[[int, int, int, str], None]


def cap_handover(text: str, limit: int = CHAR_CAP) -> str:
    """Keep the note inside the token budget the next socket will re-bill.

    A cut on a line boundary when there is one in the second half, so the
    last line the model was still writing does not land half-said. A note
    with no newline is cut hard: a broken line is still shorter than an
    essay, and shorter is the point.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    cut = clipped.rfind("\n")
    if cut >= limit // 2:
        return clipped[:cut].strip()
    return clipped.strip()


class CallMemory:
    """Transcript in, a short note out, on a cadence of caller replies."""

    def __init__(
        self,
        model: ChatModel,
        *,
        every: int = 4,
        timeout_s: float = 4.0,
        on_usage: UsageSink | None = None,
    ) -> None:
        self._model = model
        self._every = every
        self._timeout = timeout_s
        self._on_usage = on_usage
        self._lines: list[tuple[str, str]] = []
        self._agent_turn: int | None = None
        self._agent_buf = ""
        # Lines already folded into a note that landed. A failed call does
        # not advance this, so the next attempt still sees what was missed.
        self._folded = 0
        self._epoch = 0
        self.caller_turns = 0
        self.handover = ""

    def add_agent(self, text: str, turn_id: int | None = None) -> None:
        """One chunk of what Live is saying. Chunks of one turn are joined."""
        if not text:
            return
        if turn_id is not None and turn_id == self._agent_turn:
            self._agent_buf += text
            return
        self._flush_agent()
        self._agent_turn = turn_id
        self._agent_buf = text

    def add_caller(self, text: str) -> None:
        """Caller text, when something has it. Audio alone does not."""
        cleaned = text.strip()
        if not cleaned:
            return
        self._flush_agent()
        self._lines.append(("caller", cleaned))

    def caller_reply_finished(self) -> bool:
        """One spoken reply to the caller has landed. True every Nth."""
        self.caller_turns += 1
        return self._every > 0 and self.caller_turns % self._every == 0

    async def summarise(self) -> None:
        """Write the next note. Any failure leaves the previous one in place.

        CancelledError is re-raised so a call that is ending can actually
        cancel this. Everything else is swallowed: the phone call is not
        this task's to break.
        """
        try:
            await self._summarise()
        except asyncio.CancelledError:
            raise
        except Exception as failed:  # noqa: BLE001 - the call must not hear this
            log.warning("grok summary failed: %s", _safe(failed))

    def _flush_agent(self) -> None:
        if self._agent_buf.strip():
            self._lines.append(("agent", self._agent_buf.strip()))
        self._agent_buf = ""
        self._agent_turn = None

    async def _summarise(self) -> None:
        self._flush_agent()
        fresh = self._lines[self._folded :]
        if not fresh and not self.handover:
            return
        self._epoch += 1
        epoch = self._epoch
        previous = self.handover
        folded_upto = len(self._lines)
        try:
            completion = await asyncio.wait_for(
                self._model.chat(
                    _messages(fresh, previous),
                    max_tokens=MAX_TOKENS,
                ),
                self._timeout,
            )
        except asyncio.TimeoutError:
            log.warning("grok summary timed out after %.1fs", self._timeout)
            return
        except GrokError as failed:
            log.warning("grok summary failed: %s", _safe(failed))
            return

        self._record(completion)
        # A later reply already started a newer summary. Its note wins; the
        # tokens of this one are still real and were recorded above.
        if epoch != self._epoch:
            return
        text = cap_handover(completion.text)
        if not text:
            return
        self.handover = text
        self._folded = folded_upto

    def _record(self, completion: GrokCompletion) -> None:
        if self._on_usage is None:
            return
        self._on_usage(
            completion.prompt_tokens,
            completion.cached_tokens,
            completion.completion_tokens,
            completion.model,
        )


def _messages(lines: list[tuple[str, str]], previous: str) -> list[dict[str, str]]:
    body = ["Previous note:", previous or "(none)", "", "New transcript:"]
    if lines:
        body.extend(f"{role}: {text}" for role, text in lines)
    else:
        body.append("(nothing new)")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(body)},
    ]


def _safe(failed: BaseException) -> str:
    """A log line that cannot be the API key.

    xAI keys are bearer tokens and start with `xai-`. Either showing up in
    an exception string means the transport echoed a header, and the log is
    the wrong place for that.
    """
    text = f"{type(failed).__name__}"
    detail = str(failed)
    lowered = detail.lower()
    if "bearer" in lowered or "xai-" in lowered:
        return text
    if detail and detail != text:
        return f"{text}: {detail[:160]}"
    return text
