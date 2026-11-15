"""Hanging up, and the two ways of getting it wrong.

Ending a call the customer did not end is rude. Ending it mid-syllable is
worse, because the last thing they hear is the line going dead, and nothing
downstream can tell them it was deliberate.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.persona import system_instruction
from src.agent.tools.declarations import END_CALL, tool_config
from src.agent.tools.runner import ToolSession
from tests.test_catalogue import PAYLOAD, FakeResponse, catalogue_with


def session() -> ToolSession:
    cat, _ = catalogue_with(FakeResponse(PAYLOAD))
    return ToolSession(cat)


class TestTheDeclaration:
    def test_it_is_offered_to_the_model(self):
        names = [f["name"] for f in tool_config()["functionDeclarations"]]
        assert "end_call" in names

    def test_it_is_described_by_what_the_customer_said(self):
        """ "The conversation seems finished" is a judgement. "They said
        thanks" is not, and only one of those can be checked afterwards."""
        described = END_CALL["description"]
        assert isinstance(described, str)
        assert "thank you" in described
        assert "nandri" in described.lower()
        assert "never" in described.lower()

    def test_the_reason_is_one_of_two(self):
        params: Any = END_CALL["parameters"]
        assert params["properties"]["reason"]["enum"] == [
            "customer_said_goodbye",
            "handing_to_customer_care",
        ]


class TestTheFlag:
    async def test_a_fresh_session_is_not_hanging_up(self):
        assert session().hang_up is False

    async def test_end_call_raises_the_flag_and_answers(self):
        tools = session()
        result = await tools.run("end_call", {"reason": "customer_said_goodbye"})

        assert result == {"ok": True}
        assert tools.hang_up is True
        assert tools.hang_up_reason == "customer_said_goodbye"

    async def test_it_does_not_hang_up_by_itself(self):
        """A flag, not an action. The agent is still speaking when this runs."""
        tools = session()
        await tools.run("end_call", {"reason": "handing_to_customer_care"})
        assert tools.hang_up_reason == "handing_to_customer_care"

    async def test_a_reason_nobody_sent_still_records_something(self):
        tools = session()
        await tools.run("end_call", {})
        assert tools.hang_up is True
        assert tools.hang_up_reason == "customer_said_goodbye"

    async def test_searching_does_not_end_the_call(self):
        tools = session()
        await tools.run("search_products", {"category": "phone"})
        assert tools.hang_up is False


class TestThePersona:
    def test_it_is_told_when_to_call_it(self):
        said = system_instruction("voice", 15)
        assert "end_call" in said
        assert "ENDING" in said

    def test_two_products_and_never_three(self):
        """Readable on a screen, unspeakable down a phone.

        The incumbent answers a budget with five. This is the line that says
        a spoken shortlist is a different thing from a printed one.
        """
        said = system_instruction("voice", 15)
        assert "TWO products, never more" in said

    def test_the_question_names_both_choices(self):
        said = system_instruction("voice", 15)
        assert "camera mukkiyama, battery-ya?" in said

    def test_stock_is_a_count_and_never_a_branch(self):
        """Central Inventory answers a quantity and a branch COUNT.

        Before it was wired the persona said the agent could not see inside a
        branch at all, which is no longer true and was making it refuse a
        question it can now answer. It still cannot name a branch.
        """
        said = system_instruction("voice", 15)
        assert "24 branch-la" in said
        assert "cannot see which branch" in said


class TestTheDrain:
    @pytest.mark.parametrize(
        ("pcm_bytes", "rate", "expected"),
        [
            (24000 * 2 * 3, 24000, 3.0),  # three seconds of closing line
            (0, 24000, 0.0),  # nothing was spoken
            (24000 * 2 * 60, 24000, 8.0),  # capped
        ],
    )
    def test_how_long_the_line_is_held_open(
        self, pcm_bytes: int, rate: int, expected: float
    ):
        """TurnComplete means the MODEL stopped, not that the customer did.

        The audio is already across the network and queued in the page, and
        BrowserSink has no drain to await, so the wait is derived from the
        byte count - and capped, because a guess about a queue we cannot see
        must not hold a caller on a silent line.
        """
        from src.voice.session import Conversation

        seconds = pcm_bytes / (rate * 2)
        assert min(seconds, Conversation.MAX_CLOSING_WAIT_SECONDS) == expected


class TestAFloorNobodyGivesBack:
    """Found on a real call, from the trace rather than from the code.

    The greeting's socket died after five chunks of audio. TurnComplete never
    arrived, so `_agent_speaking` stayed True, so the microphone gate stayed
    shut - 1,530 gate_held events and 5 audio_out across thirty seconds, with
    the page showing "listening" and the caller talking into nothing.

    Every path out of a held gate ran through a TurnComplete that was never
    coming.
    """

    def test_a_fresh_floor_is_not_stuck(self):
        from src.voice.session import Conversation

        assert Conversation.FLOOR_HELD_TOO_LONG_SECONDS > 7.0, (
            "a real reply is about seven seconds; releasing sooner would cut "
            "the agent off mid-sentence"
        )

    def test_it_lets_go_well_inside_a_caller_s_patience(self):
        from src.voice.session import Conversation

        assert Conversation.FLOOR_HELD_TOO_LONG_SECONDS <= 30.0, (
            "the measured dead call ran thirty seconds before the caller "
            "gave up; the gate has to open before they do"
        )
