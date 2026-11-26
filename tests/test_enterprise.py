"""Release 1 hardening, Release 2 Guided, and Release 3 voice, offline."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.guided.bilingual import annotate, english_for
from src.guided.directory import ABSOLUTE_SECONDS, IDLE_SECONDS, MemoryDirectory
from src.guided.speech_cache import cache_key, chunks, prepare
from src.guided.stores import StoreRecord, handoff
from src.tools.measure_route import clopper_pearson_upper, evaluate, passes_tail
from src.tools.neutrality_scan import optional_tools, scan_svg
from src.voice.active_minute import ActiveClock, rupees_per_active_minute
from src.voice.barge import BargeIn, endpoint
from src.voice.guard import continuation, worst_case_inr
from src.voice.router import choose
from src.web.services.conversation_store import Caps, Conversation, RedisStore

ROOT = Path(__file__).resolve().parents[1]
WIDGET = ROOT / "src" / "web" / "static" / "widget"
GUIDED_JS = (ROOT / "src" / "web" / "static" / "guided.js").read_text()
PAGE = (ROOT / "src" / "web" / "static" / "index.html").read_text()


def test_paid_voice_flags_default_off(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("LIVE_VOICE", raising=False)
    monkeypatch.delenv("VOICE_ROUTE_A", raising=False)
    monkeypatch.delenv("VOICE_ROUTE_B", raising=False)
    from src.config import load

    made = load("development")
    assert made.live_voice_enabled is False
    assert made.voice_route_a_enabled is False
    assert made.voice_route_b_enabled is False


def test_widget_loader_and_panel_match_the_accessibility_contract():
    loader = (WIDGET / "sa-loader.js").read_text()
    panel = (WIDGET / "widget.js").read_text()
    assert "aria-expanded" in loader and "button.focus()" in loader
    assert "sa_conv" in loader and "restore_token" in loader
    assert "inert" in loader
    assert "aria-modal" in panel and "Escape" in panel
    assert "visualViewport" in panel
    assert "You're offline." in panel
    assert "20" in loader  # postMessage rate window
    for banned in (
        "getUserMedia",
        "AudioContext",
        "new Audio",
        "speechSynthesis",
        "WebSocket",
    ):
        assert banned not in loader
    assert "if (!el('guided-panel').hidden) guidedStart();" not in GUIDED_JS
    assert 'id="g-begin"' in PAGE
    assert 'aria-label="Speak your answer"' in PAGE
    assert "Microphone off — type instead" in GUIDED_JS
    assert "shop.geo" in GUIDED_JS


def test_optional_scan_tools_are_reported_not_fatal():
    notes = optional_tools()
    assert all(note.startswith("skipped:") or note.startswith("ran:") for note in notes)
    assert scan_svg(ROOT / "src" / "web" / "static") == []


# --- redis money -------------------------------------------------------------


def _redis() -> RedisStore:
    return RedisStore("redis://127.0.0.1:6379/0", Caps(100, 300, 30_000), "test-secret")


def _conv(host: str = "") -> Conversation:
    return Conversation(
        id="conv-redis",
        host_origin="https://www.shop.example",
        host_session=host,
        created=0,
        updated=0,
        absolute_exp=10**12,
    )


async def test_redis_reserve_settle_release_and_idempotent_rid():
    store = _redis()
    await store._safe("FLUSHDB")
    conv = _conv("hs-redis")
    first = await store.reserve(conv, "client-a", "rid-1", 40)
    again = await store.reserve(conv, "client-a", "rid-1", 40)
    assert first.ok and again.ok
    assert await store.spent_key(conv.scope_key) == 0
    settled = await store.settle("rid-1", 25)
    assert settled.ok and not settled.overrun
    assert await store.spent_key(conv.scope_key) == 25
    replay = await store.settle("rid-1", 25)
    assert replay.ok and not replay.overrun
    assert await store.spent_key(conv.scope_key) == 25
    await store.reserve(conv, "client-a", "rid-2", 10)
    await store.release("rid-2")
    assert await store.spent_key(conv.scope_key) == 25
    freed = await store.reserve(conv, "client-a", "rid-3", 70)
    assert freed.ok


async def test_redis_contention_lets_only_one_reservation_through():
    store = _redis()
    await store._safe("FLUSHDB")
    conv = _conv("hs-race")

    async def once(rid: str):
        return await store.reserve(conv, "client-b", rid, 60)

    results = await asyncio.gather(once("race-1"), once("race-2"))
    assert sum(1 for result in results if result.ok) == 1


async def test_redis_overrun_is_counted_and_a_stale_hold_is_reaped():
    store = _redis()
    await store._safe("FLUSHDB")
    conv = _conv("hs-over")
    assert (await store.reserve(conv, "client-c", "over-1", 30)).ok
    settled = await store.settle("over-1", 45)
    assert settled.overrun and store.overruns == 1
    assert (await store.reserve(conv, "client-c", "stale-1", 20)).ok
    # The hold is a few seconds old. A reaper with a zero stale window releases it.
    released = await store.reap(stale=-1)
    assert released == 1
    assert (await store.reserve(conv, "client-c", "stale-2", 20)).ok


# --- guided ------------------------------------------------------------------


def test_english_display_is_added_beside_the_tamil_line():
    view = annotate({"say": "வணக்கம்", "overview": {"text": "சரிங்க, Samsung போன்."}})
    assert view["display_lang"] == "en" and view["speech_lang"] == "ta"
    assert view["display"]["lang"] == "en"
    assert "வ" not in view["display"]["text"]
    assert view["speech"]["lang"] == "ta"
    assert english_for("The phone is ready.") == "The phone is ready."


def test_directory_expires_and_a_rank_is_charged_once():
    directory = MemoryDirectory()
    directory.open("s1", now=0)
    assert directory.touch("s1", now=10)
    assert not directory.touch("s1", now=10 + IDLE_SECONDS + 1)
    directory.open("s2", now=0)
    assert not directory.touch("s2", now=ABSOLUTE_SECONDS + 1)
    assert directory.reserve_rank("s2").ok
    assert directory.reserve_rank("s2").reason == "replay"
    directory.settle_rank("s2")
    assert not directory.reserve_rank("s2").ok
    directory.release_rank("s3")
    assert directory.claim_warm(now=0)
    assert not directory.claim_warm(now=10)


def test_speech_cache_key_is_stable_and_a_miss_is_not_spoken(tmp_path):
    assert cache_key("voice", "Hello") == cache_key("voice", "Hello")
    assert cache_key("voice", "Hello") != cache_key("other", "Hello")
    plan = prepare("Galaxy A36 costs 24499.", "ta-in", tmp_path)
    assert plan.spoken is False and plan.fallback == "details_on_screen"
    (tmp_path / f"{plan.key}.wav").write_bytes(b"RIFFxxxx")
    again = prepare("Galaxy A36 costs 24499.", "ta-in", tmp_path)
    assert again.spoken and again.cached
    assert len(chunks("One. Two?")) == 2


def test_store_handoff_uses_coordinates_not_a_brand_search():
    record = StoreRecord(
        "114",
        "Anna Nagar",
        "2nd Avenue",
        13.08,
        80.21,
        "Open until 9:30 pm",
        "2026-11-26T12:00:00+05:30",
    )
    body = handoff(record, distance_km=2.1)
    assert body["directions"] == "13.08,80.21"
    assert "query" not in body
    assert body["verified_at"]


def test_a_guest_question_does_not_call_the_model(monkeypatch):
    from src.guided.answers import Answerer
    from tests.test_guided_flow import NARROW, OneAnswer, Rig

    answers = OneAnswer()
    rig = Rig(monkeypatch, answerer=Answerer(answers))
    view = rig.walk(*NARROW)
    rig.post("recommend", session=view["session"])
    card = rig.post("pick", session=view["session"], ref="1")
    rig.now[0] += 60.0
    refused = rig.post("ask", session=card["session"], question="warranty")
    assert refused["how"]["source"] == "guest"
    assert answers.asked == 0


# --- voice -------------------------------------------------------------------


def test_the_router_stays_on_the_free_path_while_flags_are_off():
    assert choose("Is option C in stock?") == "C"
    assert choose("Which is better for night video?") == "C"
    assert choose("Which is better for night video?", route_b=True) == "B"
    assert choose("premium please", route_a=True) == "A"
    assert choose("hello") == "C"


def test_the_guard_caps_a_call_and_keeps_the_budget_on_the_ladder():
    assert worst_case_inr(0.2) == 2.5
    assert worst_case_inr(10) == 8.0
    steps = continuation({"budget": 25000, "budget_hard": True})
    assert [step.step for step in steps] == ["templated", "micro", "text", "handover"]
    assert "25000" in steps[-1].note
    assert steps[-1].slots["budget"] == 25000


def test_active_minutes_exclude_idle_and_stop_at_barge_in():
    clock = ActiveClock()
    clock.shopper_spoke(0, 1)
    clock.waited(1, 1.4)
    clock.assistant_spoke(1.4, 4.0, barge_at=2.0)
    clock.listening_grace(2.0, 20.0)
    clock.hidden = True
    clock.shopper_spoke(30, 40)
    assert clock.active_seconds() == pytest.approx(1.3 + 0.4 + 0.6 + 5.0)
    assert rupees_per_active_minute(7.3, clock.active_seconds()) == pytest.approx(60.0)
    assert endpoint(400, silence_ms=300)
    assert not endpoint(400, silence_ms=100)
    barge = BargeIn(at_ms=2000, flush_ms=80)
    assert barge.in_time and barge.event()["type"] == "barge_in"


def test_the_tail_gate_passes_eight_exceedances_in_three_hundred_calls():
    assert passes_tail(8, 300)
    assert not passes_tail(9, 300)
    assert not passes_tail(0, 50)
    assert clopper_pearson_upper(0, 59) < 0.05
    cheap = [
        {
            "route": "C",
            "inr_per_active_min": 0.3,
            "display": "The phone is in stock.",
            "quoted_price": 19999,
            "price": 19999,
            "stock_state": "in_stock",
            "said_stock": "In stock",
            "budget": 25000,
        }
        for _ in range(300)
    ]
    assert evaluate(cheap)["go"]
    poisoned = dict(cheap[0])
    poisoned["stock_state"] = "unconfirmed"
    poisoned["said_stock"] = "Out of stock"
    report = evaluate([poisoned])
    assert report["false_out_of_stock"] == 1 and report["go"] is False
