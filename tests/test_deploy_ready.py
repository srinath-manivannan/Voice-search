"""What a public deploy with no database and no speech server relies on
(owner, 2026-11-25): one ceiling on everything paid per day, the mic only
where the browser hears, the estate shipped in the image, and a readiness
signal that says when recommendations will not wait."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from src.web.services.spend_ledger import DailyLedger
from tests.test_guided_flow import WIDE, Rig


@pytest.fixture
def rig(monkeypatch) -> Rig:
    return Rig(monkeypatch)


def test_the_daily_ceiling_holds_and_starts_again_at_midnight():
    day = [dt.date(2026, 11, 25)]
    ledger = DailyLedger(10.0, today=lambda: day[0])

    assert ledger.allows(4.0)
    ledger.add(7.0)
    assert ledger.allows(3.0) and not ledger.allows(3.5)
    ledger.add(3.0)
    assert not ledger.allows(), "spent: nothing paid starts"

    day[0] = dt.date(2026, 11, 26)
    assert ledger.allows(9.0) and ledger.spent_inr() == 0.0


def test_a_ceiling_of_zero_lets_nothing_paid_start():
    assert not DailyLedger(0.0).allows()


def test_a_live_call_is_refused_with_its_reason_once_the_day_is_spent(monkeypatch):
    monkeypatch.delenv("LIVE_VOICE", raising=False)
    fresh = Rig(monkeypatch)
    fresh.client.app.state.ledger.add(10_000.0)

    with pytest.raises(WebSocketDisconnect) as refused:
        with fresh.client.websocket_connect("/ws") as socket:
            socket.receive_text()
    assert refused.value.code == 1008
    assert "Guided" in refused.value.reason


def test_the_guided_think_falls_back_to_rules_when_the_day_is_spent(rig: Rig):
    rig.client.app.state.guided.ledger = DailyLedger(0.0)
    view = rig.walk(*WIDE)
    rig.verify(view["session"])
    rig.now[0] += 60.0

    result = rig.post("recommend", session=view["session"])

    assert result["how"]["source"] == "rules" and rig.thinker.briefs == []
    assert result["meter"]["total_inr"] == 0.0


def test_a_paid_think_is_added_to_the_days_spend(rig: Rig):
    ledger = DailyLedger(100.0)
    rig.client.app.state.guided.ledger = ledger
    view = rig.walk(*WIDE)
    rig.verify(view["session"])
    rig.now[0] += 60.0

    rig.post("recommend", session=view["session"])

    assert ledger.spent_inr() == pytest.approx(0.074, abs=0.001)


def test_no_speech_server_means_no_transcribe_and_the_page_is_told(rig: Rig):
    config = rig.client.get("/api/v1/config").json()
    assert config["hosted_asr"] is False

    view = rig.post("start")
    response = rig.client.post(
        f"/api/v1/guided/transcribe?session={view['session']}&lang=ta",
        content=b"\x00\x01" * 8000,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 404


def test_readiness_says_when_recommendations_will_not_wait(rig: Rig):
    before = rig.client.get("/readyz").json()
    assert before["warm"] is False

    view = rig.walk(("shelf", "phone"))
    assert view["question"]["id"] == "brand"
    after = rig.client.get("/readyz").json()
    assert after["warm"] is True


def test_the_image_ships_the_estate_and_no_speech_synthesiser():
    docker = Path("Dockerfile").read_text()
    assert "COPY vendor ./vendor" in docker
    assert "espeak" not in docker
    for name in (
        "worker/product.py",
        "worker/apis.py",
        "worker/adapters.py",
        "tools/worker_shim.py",
    ):
        assert Path("vendor/estate/src", name).is_file(), name
    assert Path("vendor/estate/SOURCE").read_text().startswith("search-proxy ")
