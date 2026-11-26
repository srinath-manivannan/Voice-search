"""The handover Grok writes, and what happens when it does not write one."""

from __future__ import annotations

import asyncio

import pytest

from src.agent.tools.runner import ToolSession
from src.agent.tools.shortlist import Candidate
from src.memory.grok import GrokCompletion, GrokError
from src.memory.summariser import CHAR_CAP, CallMemory, cap_handover
from src.voice.cost import GROK, grok_rates, grok_usd
from src.voice.handover import note, with_memory
from src.voice.usage import SessionUsage


class FakeGrok:
    def __init__(
        self,
        text: str = "Budget:\n20-25k\nLanguage: Tanglish",
        fail: Exception | None = None,
    ) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0
        self.model = "grok-4.3"
        self.seen: list[list[dict[str, str]]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> GrokCompletion:
        self.calls += 1
        self.seen.append(messages)
        assert max_tokens == 150
        if self.fail is not None:
            raise self.fail
        return GrokCompletion(
            text=self.text,
            prompt_tokens=80,
            cached_tokens=10,
            completion_tokens=20,
            model=self.model,
        )


def memory(model: FakeGrok, every: int = 4) -> tuple[CallMemory, SessionUsage]:
    usage = SessionUsage()
    remembered = CallMemory(
        model,
        every=every,
        timeout_s=0.2,
        on_usage=lambda prompt, cached, completion, name: usage.add_grok(
            prompt, cached, completion, name
        ),
    )
    return remembered, usage


def feed(remembered: CallMemory, n: int) -> None:
    for i in range(n):
        remembered.add_caller(f"phone under {20000 + i}")
        remembered.add_agent("Poco, 19999.", turn_id=i)


async def test_the_fourth_caller_reply_is_the_first_summary():
    model = FakeGrok()
    remembered, usage = memory(model)
    feed(remembered, 3)
    assert remembered.caller_reply_finished() is False
    assert remembered.caller_reply_finished() is False
    assert remembered.caller_reply_finished() is False
    assert model.calls == 0

    assert remembered.caller_reply_finished() is True
    await remembered.summarise()

    assert model.calls == 1
    assert "Tanglish" in remembered.handover
    assert usage.grok_prompt_tokens == 80
    assert usage.grok_completion_tokens == 20
    # 70 fresh at $1.25 plus 10 cached at $0.20 plus 20 out at $2.50.
    assert usage.grok_usd == pytest.approx(grok_usd(80, 10, 20, grok_rates("grok-4.3")))


async def test_a_failed_summary_keeps_the_note_the_call_already_had():
    model = FakeGrok(fail=GrokError("grok returned 500"))
    remembered, usage = memory(model)
    remembered.handover = "Budget:\nkept"
    feed(remembered, 1)
    remembered.caller_reply_finished()
    await remembered.summarise()

    assert remembered.handover == "Budget:\nkept"
    assert usage.grok_tokens == 0


async def test_a_timeout_is_not_a_new_note():
    class Slow(FakeGrok):
        async def chat(
            self, messages: list[dict[str, str]], *, max_tokens: int
        ) -> GrokCompletion:
            await asyncio.sleep(1)
            return await super().chat(messages, max_tokens=max_tokens)

    remembered, _usage = memory(Slow(), every=1)
    remembered.handover = "still here"
    remembered.add_agent("hello", turn_id=1)
    remembered.caller_reply_finished()
    await remembered.summarise()
    assert remembered.handover == "still here"


async def test_the_note_is_capped_and_sits_beside_the_tool_note():
    long = "Offered:\n" + ("phone " * 400)
    assert len(cap_handover(long)) <= CHAR_CAP
    assert cap_handover("Budget:\n20k") == "Budget:\n20k"

    session = ToolSession(None)
    session._shortlist = [
        Candidate(
            ref="1",
            item_code="1",
            name="Poco M8",
            price=19999,
            was=None,
            brand=None,
            in_stock_hint=True,
            specs={},
            rating=None,
        )
    ]
    session._budget = (20000, 25000)
    combined = with_memory(session, "Language: Tanglish")
    assert combined.startswith(note(session))
    assert combined.endswith("Language: Tanglish")
    assert "19,999" in combined
    assert with_memory(session, "  ") == note(session)


def test_the_grok_line_stays_off_the_bill_until_memory_is_on():
    usage = SessionUsage()
    plain = usage.breakdown(88.0, 0.18, 1.0)
    assert all(line.key != GROK for line in plain.lines)
    shown = usage.breakdown(88.0, 0.18, 1.0, include_grok=True)
    assert shown.lines[-1].key == GROK
    assert shown.lines[-1].inr_per_min == 0
    assert shown.total_inr_per_min == plain.total_inr_per_min


async def test_the_cost_line_uses_the_grok_card_and_gst():
    usage = SessionUsage()
    usage.add_grok(750, 0, 120, "grok-4.3")
    # One summary. The per-minute figure divides by the minute we hand it.
    breakdown = usage.breakdown(88.0, 0.18, 1.0, include_grok=True)
    line = breakdown.line(GROK)
    expected = grok_usd(750, 0, 120, grok_rates("grok-4.3"))
    assert line.usd == pytest.approx(expected)
    assert line.inr_per_min == pytest.approx(expected * 88 * 1.18)
    assert breakdown.total_usd == pytest.approx(expected)
    assert line.label == "Grok memory"


def test_an_unknown_model_is_priced_on_the_dearer_card():
    usage = SessionUsage()
    usage.add_grok(1000, 0, 100, "grok-not-a-model")
    dear = grok_usd(1000, 0, 100, grok_rates("grok-4.7"))
    assert usage.grok_usd == pytest.approx(dear)


async def test_a_later_summary_wins_when_two_overlap():
    gate = asyncio.Event()

    class Gated(FakeGrok):
        async def chat(
            self, messages: list[dict[str, str]], *, max_tokens: int
        ) -> GrokCompletion:
            self.calls += 1
            if self.calls == 1:
                await gate.wait()
            return GrokCompletion(
                "second" if self.calls == 2 else "first", 1, 0, 1, "grok-4.3"
            )

    model = Gated()
    remembered, _usage = memory(model, every=1)
    remembered.add_agent("one", turn_id=1)
    first = asyncio.create_task(remembered.summarise())
    await asyncio.sleep(0)
    remembered.add_agent("two", turn_id=2)
    await remembered.summarise()
    gate.set()
    await first
    assert remembered.handover == "second"
