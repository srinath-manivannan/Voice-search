"""Rotating a call onto a fresh socket, and what crosses with it.

The failures this guards against are all quiet ones: a rotation in the wrong
place closes the socket, a turn id that goes backwards silences a live turn,
and a note that forgets the budget produces an agent that offers a phone a
third over what was asked for. None of them looks like an exception.
"""

from __future__ import annotations

import asyncio


import pytest

from src.agent.tools.runner import ToolSession
from src.agent.tools.shortlist import Candidate
from src.voice.events import AudioOut, Event, TurnComplete, TurnInterrupted
from src.voice.handover import note
from src.voice.rotating import RotatingLiveSession
from src.voice.usage import TurnUsage


def spoke(turn: int, prompt_audio: int, response_audio: int = 120) -> TurnComplete:
    return TurnComplete(
        turn_id=turn,
        usage=TurnUsage(prompt_audio=prompt_audio, response_audio=response_audio),
        first_audio_ms=500.0,
    )


class FakeLive:
    """One socket. Records what it was told, yields what it was given."""

    opened = 0

    def __init__(self, script: list[Event]) -> None:
        self._script = script
        self.primed: str | None = None
        self.greeted: str | None = None
        self.audio: list[bytes] = []
        self.ends = 0
        self.closed = False
        FakeLive.opened += 1

    async def __aenter__(self) -> FakeLive:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def prime(self, context: str) -> None:
        self.primed = context

    async def greet(self, text: str) -> None:
        self.greeted = text

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def end_of_speech(self) -> None:
        self.ends += 1

    async def events(self):
        for event in self._script:
            yield event


def rotating(scripts, note_text="carried", threshold=600):
    """A session whose nth socket plays the nth script."""
    made: list[FakeLive] = []

    def make():
        session = FakeLive(scripts[len(made)] if len(made) < len(scripts) else [])
        made.append(session)
        return session

    session = RotatingLiveSession(
        make, lambda: note_text, rotate_above_audio_tokens=threshold
    )
    return session, made


async def drain(session) -> list[Event]:
    return [event async for event in session.events()]


# --- when to rotate ---------------------------------------------------------


async def test_it_rotates_once_the_retained_audio_is_expensive():
    session, made = rotating([[spoke(1, 200), spoke(2, 700)], [spoke(1, 150)]])
    async with session:
        await drain(session)
    assert session.rotations == 1
    assert made[0].closed
    assert made[1].primed == "carried"


async def test_it_stays_put_while_the_history_is_still_cheap():
    session, made = rotating([[spoke(1, 100), spoke(2, 200), spoke(3, 300)]])
    async with session:
        await drain(session)
    assert session.rotations == 0


async def test_a_threshold_of_zero_means_one_socket_for_the_whole_call():
    """Which is the behaviour this replaces, kept reachable by one setting."""
    session, made = rotating([[spoke(1, 5000)]], threshold=0)
    async with session:
        await drain(session)
    assert session.rotations == 0


async def test_it_never_rotates_between_a_tool_call_and_its_answer():
    """The silent half of a tool call is a TurnComplete with no audio.

    The function response has gone out on this socket and the answer has not
    come back. Rotating there strands the pair across two sockets, and the
    API answers that by closing the connection - 1007, which is what killed
    every call on 2026-11-10.
    """
    session, made = rotating(
        [[spoke(1, 900, response_audio=0), spoke(1, 900)], [spoke(1, 100)]]
    )
    async with session:
        await drain(session)
    # Exactly one rotation, and it happened on the turn that spoke.
    assert session.rotations == 1


# --- what the caller must not notice ----------------------------------------


async def test_turn_ids_keep_rising_across_a_rotation():
    """A consumer drops audio for abandoned turns BY ID. An id that went
    backwards would silence a turn that is still being spoken."""
    session, _ = rotating(
        [[spoke(1, 200), spoke(2, 700)], [spoke(1, 100), spoke(2, 100)]]
    )
    async with session:
        events = await drain(session)
    ids = [event.turn_id for event in events]
    assert ids == sorted(ids), ids
    assert len(set(ids)) == len(ids), "two turns shared an id across the swap"


async def test_only_the_first_socket_greets():
    """A rotation the customer notices is a rotation that failed, and the
    greeting is the loudest way to notice one."""
    session, made = rotating([[spoke(1, 700)], [spoke(1, 100)]])
    async with session:
        await session.greet("Vanakkam!")
        await drain(session)
    assert made[0].greeted == "Vanakkam!"
    assert made[1].greeted is None
    assert made[1].primed == "carried"


async def test_audio_goes_to_whichever_socket_is_current():
    session, made = rotating([[spoke(1, 700)], [spoke(1, 100)]])
    async with session:
        await session.send_audio(b"before")
        await drain(session)
        await session.send_audio(b"after")
    assert made[0].audio == [b"before"]
    assert made[1].audio == [b"after"]


async def test_the_cost_of_every_socket_lands_in_one_place():
    """Otherwise the bill for a call is however much the last socket cost."""
    session, _ = rotating(
        [[spoke(1, 200), spoke(2, 700)], [spoke(1, 100), spoke(2, 150)]]
    )
    async with session:
        await drain(session)
    assert len(session.usage.turns) == 4
    assert session.usage.billed_in_audio == 200 + 700 + 100 + 150


async def test_a_spare_that_will_not_connect_does_not_end_the_call():
    """The current socket still works. It is only expensive, and an
    expensive call is better than a dropped one."""
    attempts = {"n": 0}

    def make():
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise RuntimeError("no spare today")
        return FakeLive([spoke(1, 700)])

    session = RotatingLiveSession(
        make, lambda: "carried", rotate_above_audio_tokens=600
    )
    async with session:
        await drain(session)
    assert session.rotations == 0


# --- what crosses -----------------------------------------------------------


def tool_session_with(budget=None, shortlist=(), discussed=None) -> ToolSession:
    session = ToolSession(None, None)
    session._budget = budget
    session._shortlist = list(shortlist)
    session._discussed = discussed
    return session


def phone(ref: str, name: str, price: int | None) -> Candidate:
    return Candidate(
        ref=ref,
        item_code=f"code-{ref}",
        name=name,
        price=price,
        was=None,
        brand=None,
        in_stock_hint=True,
        specs={},
        rating=None,
    )


def test_the_note_carries_the_budget_compression_always_lost():
    """A budget is said once, at the start - exactly the turn a sliding
    window drops first. Eight turns into a probe the agent had lost it."""
    carried = note(tool_session_with(budget=(20000, 25000)))
    assert "20,000" in carried and "25,000" in carried


def test_the_note_carries_the_shortlist_so_the_second_one_still_means_one():
    carried = note(
        tool_session_with(
            budget=(20000, 25000),
            shortlist=(phone("1", "Poco M8 5G", 24999), phone("2", "Realme P4", 20999)),
        )
    )
    assert "Poco M8 5G at 24,999" in carried
    assert "Realme P4 at 20,999" in carried


def test_the_note_says_which_one_they_had_narrowed_to():
    carried = note(
        tool_session_with(
            shortlist=(phone("1", "Poco M8 5G", 24999),),
            discussed=phone("1", "Poco M8 5G", 24999),
        )
    )
    assert "asking about the Poco M8 5G" in carried


def test_the_note_tells_the_new_socket_not_to_greet():
    carried = note(tool_session_with(budget=(None, 30000)))
    assert "not greet" in carried


def test_nothing_to_carry_carries_nothing():
    """Rotating before the customer has said anything worth remembering
    should hand over nothing, rather than a sentence claiming a
    conversation that did not happen."""
    assert note(tool_session_with()) == ""


async def test_an_empty_note_is_never_sent():
    session, made = rotating([[spoke(1, 700)], [spoke(1, 100)]], note_text="   ")
    async with session:
        await drain(session)
    # It rotated, and primed the new socket with the empty note - which
    # GeminiLiveSession.prime() then declines to put on the wire.
    assert session.rotations == 1
    from src.voice.gemini_live import GeminiLiveSession

    sent: list[str] = []

    class Socket:
        async def send(self, data: str) -> None:
            sent.append(data)

    live = GeminiLiveSession.__new__(GeminiLiveSession)
    live._socket = Socket()  # type: ignore[attr-defined]
    await live.prime("   ")
    assert sent == []


def test_the_shipped_threshold_is_about_two_exchanges():
    """Sized from the probe: prompt_audio reached 778 after two content
    turns, so 600 rotates at roughly every second exchange. Low enough to
    bite, high enough that a rotation is not happening every turn.
    """
    from src.config import devconfig, prodconfig

    for defaults in (devconfig, prodconfig):
        assert 250 <= defaults.ROTATE_ABOVE_AUDIO_TOKENS <= 1500


@pytest.mark.parametrize("event", [AudioOut(turn_id=3, pcm=b"x"), TurnInterrupted(3)])
async def test_every_event_type_survives_renumbering(event):
    """A match statement that misses a case returns None and the consumer
    sees an event that is not one."""
    session, _ = rotating([[event, spoke(4, 100)]])
    async with session:
        events = await drain(session)
    assert type(events[0]) is type(event)


# --- the spare's lifetime ---------------------------------------------------
#
# Found on the first live run and not before it: a spare opened at the start
# of a call is closed by the server long before it is used - "1008 Session
# closed due to client inactivity" - and the call dies at the rotation the
# spare existed to make invisible.


async def test_no_spare_is_opened_before_the_history_is_anywhere_near_due():
    """One opened at the start spends the whole call idle, and an idle Live
    socket is one the server closes."""
    FakeLive.opened = 0
    session, _ = rotating([[spoke(1, 100), spoke(2, 150)]])
    async with session:
        await drain(session)
    assert FakeLive.opened == 1, "opened a spare it did not need"


async def test_the_spare_is_opened_as_the_threshold_comes_into_view():
    """Late enough to be seconds old when used, early enough that the 1.7
    seconds of connecting lands inside a reply still being spoken."""
    FakeLive.opened = 0
    # 450 is 75% of 600: past the warm mark, short of the rotation.
    session, _ = rotating([[spoke(1, 450), spoke(2, 500)]])
    async with session:
        await drain(session)
    assert FakeLive.opened == 2, "no spare was waiting when the threshold neared"
    assert session.rotations == 0, "it rotated before it had to"


async def test_a_waiting_spare_is_kept_alive():
    """Silence is not billed - Phase 0 measured that - so a frame of it
    every few seconds costs nothing and keeps the socket open."""
    session, made = rotating([[spoke(1, 450)]])
    session.KEEPALIVE_SECONDS = 0.01
    async with session:
        await drain(session)
        await asyncio.sleep(0.05)
        spare = made[1]
        assert spare.audio, "the spare was left to be closed for inactivity"
        assert set(spare.audio[0]) == {0}, "a keepalive must carry no speech"


async def test_the_keepalive_stops_once_the_spare_is_in_service():
    """Otherwise two tasks are writing to the socket the caller is on."""
    session, made = rotating([[spoke(1, 450), spoke(2, 700)], [spoke(1, 100)]])
    session.KEEPALIVE_SECONDS = 0.01
    async with session:
        await drain(session)
        assert session.rotations == 1
        before = len(made[1].audio)
        await asyncio.sleep(0.05)
        assert len(made[1].audio) == before, "still sending keepalives into a live call"


async def test_the_swap_happens_before_the_consumer_hears_the_turn_ended():
    """The race that cost two live runs.

    The other task sends audio and end-of-speech the moment it sees a
    TurnComplete. If the swap happens after that event is yielded, the
    caller's next turn lands on the socket being retired, is answered into
    a stream nobody reads, and the API ends it a minute later as "client
    inactivity". So by the time TurnComplete reaches the consumer, the new
    socket must already be the current one.
    """
    session, made = rotating([[spoke(1, 700)], [spoke(1, 100)]])
    async with session:
        sent = False
        current_when_told = None
        async for event in session.events():
            if isinstance(event, TurnComplete) and not sent:
                current_when_told = session._current
                # Whatever the consumer sends now must reach the new socket.
                await session.send_audio(b"next turn")
                sent = True
    assert session.rotations == 1
    assert made[0].audio == [], "audio went to the socket being retired"
    assert made[1].audio == [b"next turn"]
    assert current_when_told is made[1], "the swap had not happened yet"
