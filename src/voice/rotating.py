"""One conversation across several sockets, so history stops being audio.

The Live API bills every token in the window on every generation, and on a
voice call the window is mostly audio at four times the text rate. The same
seconds of speech are therefore paid for again, and again, for as long as
they stay. Compression is the documented answer and it is a poor one here:
its own minimum trigger is larger than a three-minute call accumulates, and
anything small enough to bite either empties the window of the conversation
or - at 400 tokens - closes the socket on the first tool call.

So: when the retained audio gets expensive, move the conversation to a fresh
socket and hand it a few sentences of text instead. Measured 2026-11-12,
`prompt_audio` 778 and climbing before, 222 and flat after, with the agent
still naming the right phone and still remembering the budget.

Three things make it invisible to the caller:

- **The spare is already connected.** Opening a socket takes about 1.7
  seconds, which is far too long between turns and comfortably short inside
  one, since the agent speaks for about seven. The swap itself is one send.
  It is opened LATE and kept warm, both learned the hard way: a spare opened
  at the start of a call was closed by the server before it was ever used -
  `1008 Session closed due to client inactivity` - and the call died at the
  rotation it was supposed to make invisible.
- **It only happens after the agent has finished speaking.** Never between a
  function call and its response: that pair must stay on one socket, and
  splitting it is the 1007 that killed every call on 2026-11-10.
- **The new socket is told not to greet.** A rotation the customer notices
  is a rotation that failed.

This class presents exactly the surface `Conversation` uses, so nothing
above it knows there is more than one socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from src.voice.events import (
    AudioOut,
    CallerTranscript,
    Event,
    Transcript,
    TurnComplete,
    TurnInterrupted,
)
from src.voice.gemini_live import GeminiLiveSession
from src.voice.usage import SessionUsage

log = logging.getLogger(__name__)

Make = Callable[[], GeminiLiveSession]
Note = Callable[[], str]


class RotatingLiveSession:
    """A Live conversation that changes sockets when its history gets dear."""

    def __init__(
        self,
        make: Make,
        note: Note,
        *,
        rotate_above_audio_tokens: int,
    ) -> None:
        self._make = make
        self._note = note
        # Zero switches rotation off entirely and leaves one socket for the
        # whole call, which is the behaviour this replaces.
        self._threshold = rotate_above_audio_tokens
        self._current: GeminiLiveSession | None = None
        self._spare: asyncio.Task[GeminiLiveSession] | None = None
        self._warming: asyncio.Task[None] | None = None
        # Held across a swap. send_audio runs fifty times a second from the
        # other task, and a frame handed to a socket that is being replaced
        # is a frame lost - an uncontended asyncio lock costs nothing.
        self._swapping = asyncio.Lock()
        # Turn ids must keep rising across sockets: a consumer drops audio
        # for abandoned turns by id, and an id that went backwards would
        # silence a live turn.
        self._turn_base = 0
        self._last_inner_turn = 0
        self._closed = False
        # The whole call's cost, across every socket it used.
        self.usage = SessionUsage()
        self.rotations = 0

    # -- the surface Conversation uses --------------------------------------

    @property
    def turn_id(self) -> int:
        return self._turn_base + self._last_inner_turn

    async def __aenter__(self) -> RotatingLiveSession:
        self._current = await self._make().__aenter__()
        # No spare yet. One opened now would spend the whole call idle, and
        # the server closes an idle session - which is how the first live
        # run of this died at its first rotation.
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._closed = True
        if self._warming is not None:
            self._warming.cancel()
            await asyncio.gather(self._warming, return_exceptions=True)
            self._warming = None
        if self._spare is not None:
            self._spare.cancel()
            spare = await asyncio.gather(self._spare, return_exceptions=True)
            if isinstance(spare[0], GeminiLiveSession):
                await self._close(spare[0])
        if self._current is not None:
            await self._current.__aexit__(*exc)
            self._current = None

    async def send_audio(self, pcm: bytes) -> None:
        async with self._swapping:
            if self._current is not None:
                await self._current.send_audio(pcm)

    async def end_of_speech(self) -> None:
        async with self._swapping:
            if self._current is not None:
                await self._current.end_of_speech()

    async def greet(self, text: str) -> None:
        """Only the first socket greets. Every later one is told not to."""
        async with self._swapping:
            if self._current is not None:
                await self._current.greet(text)

    async def prime(self, context: str) -> None:
        """Add context without asking for an answer.

        Rotation uses this on a fresh socket. A prefetch uses it mid-call, on
        whichever socket is current - through the same lock, because landing a
        prefetched shortlist on a socket that is being retired puts it in a
        stream nobody is reading.
        """
        async with self._swapping:
            if self._current is not None:
                await self._current.prime(context)

    async def say(self, text: str) -> None:
        """Put words in the caller's mouth. Measurement harnesses only.

        A real call arrives as audio through `send_audio`; this exists so a
        conversation can be driven without a microphone, and it goes through
        the same lock so it lands on whichever socket is current.
        """
        async with self._swapping:
            if self._current is not None:
                await self._current.say(text)

    async def events(self) -> AsyncIterator[Event]:
        """Yield from the current socket, and keep going across a change.

        The STREAM is held, not the session. Breaking out of an async
        generator suspends it; iterating the same object again resumes where
        it stopped, while asking the session for `events()` a second time
        would start a new one. That distinction is the difference between
        carrying on after a rotation that could not happen and replaying the
        call from the beginning, forever.
        """
        if self._current is None:
            return
        stream = self._current.events()
        while not self._closed:
            rotated = False
            async for event in stream:
                # Renumbered BEFORE any swap, so it carries the ids of the
                # socket it came from.
                out = self._renumber(event)
                if isinstance(event, TurnComplete):
                    self.usage.turns.append(event.usage)
                    if self._nearly_due(event):
                        self._open_spare()
                    if self._should_rotate(event):
                        # Swapped before the consumer is told the turn
                        # ended. The other task sends audio and end-of-speech
                        # the moment it hears that, and anything landing on
                        # the socket we are leaving is answered into a
                        # stream nobody is reading - which the API ends as
                        # "client inactivity" a minute later.
                        rotated = await self._rotate()
                        yield out
                        if rotated:
                            break
                        continue
                yield out
            if self._closed or not rotated:
                # The socket ended on its own: the call is over, or it
                # dropped. Either way this is not ours to restart.
                return
            # Breaking out of an async generator suspends it; closing it
            # releases the socket we have finished with.
            if (close := getattr(stream, "aclose", None)) is not None:
                await close()
            assert self._current is not None
            stream = self._current.events()

    # -- the mechanics ------------------------------------------------------

    def _renumber(self, event: Event) -> Event:
        """Inner turn ids restart at zero on a new socket; ours do not."""
        inner = event.turn_id
        self._last_inner_turn = max(self._last_inner_turn, inner)
        outer = self._turn_base + inner
        match event:
            case AudioOut():
                return AudioOut(turn_id=outer, pcm=event.pcm)
            case Transcript():
                return Transcript(turn_id=outer, text=event.text)
            case CallerTranscript():
                return CallerTranscript(turn_id=outer, text=event.text)
            case TurnInterrupted():
                return TurnInterrupted(turn_id=outer)
            case TurnComplete():
                return TurnComplete(
                    turn_id=outer,
                    usage=event.usage,
                    first_audio_ms=event.first_audio_ms,
                )

    def _should_rotate(self, event: TurnComplete) -> bool:
        """Only when the agent has actually just spoken.

        A generation that produced no audio is the silent half of a tool
        call: the function response has gone out and the answer has not come
        back. Rotating there strands the pair on two sockets, which the API
        answers by closing the connection.
        """
        if self._threshold <= 0 or self._closed:
            return False
        if event.usage.response_audio == 0:
            return False
        return event.usage.prompt_audio >= self._threshold

    # How close to the threshold before the spare is opened. Late enough
    # that it is seconds old when it is used, early enough that the 1.7
    # seconds of connecting lands inside a reply the agent is still speaking.
    WARM_AT = 0.7
    # A Live socket with nothing arriving on it is closed by the server.
    # Silence is not billed - Phase 0 measured that - so a frame of it every
    # few seconds keeps the spare alive for what it costs to send.
    KEEPALIVE_SECONDS = 5.0
    KEEPALIVE_FRAME = b"\x00" * 640  # 20 ms at 16 kHz, 16-bit mono

    def _nearly_due(self, event: TurnComplete) -> bool:
        if self._threshold <= 0 or self._closed or self._spare is not None:
            return False
        return event.usage.prompt_audio >= self._threshold * self.WARM_AT

    def _open_spare(self) -> None:
        """Start connecting the next socket, now, in the background."""
        if self._closed or self._spare is not None:
            return

        async def connect() -> GeminiLiveSession:
            session = self._make()
            await session.__aenter__()
            return session

        self._spare = asyncio.create_task(connect(), name="live-spare")
        self._warming = asyncio.create_task(self._keep_warm(), name="live-warm")

    async def _keep_warm(self) -> None:
        """Hold the spare open until it is needed.

        Without this a spare that waits longer than the server's idle
        timeout is closed underneath us, and the rotation it was opened for
        takes the call down with it.
        """
        spare = self._spare
        if spare is None:
            return
        try:
            session = await spare
            while not self._closed:
                await asyncio.sleep(self.KEEPALIVE_SECONDS)
                if self._spare is not spare:
                    return  # it has been taken into service
                await session.send_audio(self.KEEPALIVE_FRAME)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a spare, not the live call
            log.debug("could not keep the spare warm", exc_info=True)

    async def _rotate(self) -> bool:
        """Move to the spare, hand it the note, retire the old socket.

        False if there was no spare to move to.
        """
        if self._spare is None:
            self._open_spare()
        assert self._spare is not None
        if self._warming is not None:
            self._warming.cancel()
            await asyncio.gather(self._warming, return_exceptions=True)
            self._warming = None
        try:
            fresh = await self._spare
        except Exception:
            # A spare that would not connect is not worth ending a call
            # over: the current socket still works, it is only expensive.
            log.exception("could not open a spare socket; staying put")
            self._spare = None
            self._open_spare()
            return False
        self._spare = None

        note = self._note()
        retiring = self._current
        async with self._swapping:
            await fresh.prime(note)
            self._current = fresh
            # Everything the old socket numbered is now behind us.
            self._turn_base += self._last_inner_turn
            self._last_inner_turn = 0
        self.rotations += 1
        log.info(
            "rotated onto a fresh socket (%d so far), carried %d characters",
            self.rotations,
            len(note),
        )
        if retiring is not None:
            await self._close(retiring)
        self._open_spare()
        return True

    @staticmethod
    async def _close(session: GeminiLiveSession) -> None:
        try:
            await session.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - a socket we are done with
            log.debug("retiring socket did not close cleanly", exc_info=True)
