"""Reading a fact off an upstream payload, and the one error a customer sees.

Nothing here builds a ToolSession - this is the parsing, on its own, against
real captured responses in tests/payloads.py. The tools end to end are in
tests/test_tool_session.py.
"""

from __future__ import annotations

import pytest

from src.agent.tools import estate as estate_api
from tests.payloads import ANSWERED


def test_a_service_that_could_not_be_asked_is_not_an_empty_shelf():
    """The one error here a customer can see.

    Production cannot reach Central Inventory, and reading that as zero is how
    a stocked phone gets a Notify Me button. `unknown` and `none` must stay
    different all the way to the sentence.
    """
    facts = estate_api.read(ANSWERED)

    assert facts.stock.state == estate_api.UNKNOWN
    assert facts.stock.in_stock is None
    assert "rejects both published tokens" in (facts.stock.reason or "")
    assert facts.unanswered == (estate_api.STOCK,)


def test_an_unreadable_body_arriving_as_http_200_is_unknown():
    """`{"message": "signature is invalid"}` comes back with HTTP 200.

    The CI gateway verifies its token with a secret chosen by the `mode`
    header, so the same token is accepted under one mode and refused under
    another - and the refusal is a 200 with no stock figure in it.
    """
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["stock"] = {
        "status": "invalid",
        "reason": "signature is invalid",
    }

    stock = estate_api.read(payload).stock
    assert stock.state == estate_api.UNKNOWN
    assert stock.in_stock is None


def test_stock_that_answered_zero_is_out_of_stock_and_not_unknown():
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["stock"] = {
        "status": "ok",
        "data": {"in_stock": False, "quantity": 0, "branch_count": 0},
    }

    stock = estate_api.read(payload).stock
    assert (stock.state, stock.in_stock, stock.quantity) == (
        estate_api.KNOWN,
        False,
        0,
    )


def test_an_emi_availability_row_is_not_a_discount():
    """It arrives with a null amount and a 0% discount.

    Counting it as an offer is how an agent invents a saving, and it is the
    only offer on most products - so the agent would announce a discount on
    nearly everything.
    """
    offers = estate_api.read(ANSWERED).offers

    assert [o.label for o in offers.items] == [
        "Bank Offer : Get 2000 Instant Discount On HDFC Bank Cards"
    ]
    assert offers.best_amount == 2000
    assert offers.effective_price == 32999


def test_an_offer_keeps_the_card_that_makes_it_apply():
    """ "2,000 off irukku" is false. "HDFC card-la" is the whole claim."""
    offer = estate_api.read(ANSWERED).offers.items[0]

    assert offer.condition == "debit/credit card on emi"
    assert offer.lowers_price is True


def test_offers_are_alternatives_and_never_a_total():
    assert estate_api.read(ANSWERED).offers.stacks is False


def test_the_emi_headline_is_the_one_the_shop_quotes():
    """Not the smallest instalment of the three kinds.

    A No Cost plan over 6 months has the largest instalment and is the
    cheapest offer on the page, so "smallest wins" is wrong. The upstream
    picks Standard, which is what poorvika.com prints.
    """
    emi = estate_api.read(ANSWERED).emi

    assert emi.state == estate_api.KNOWN
    assert (emi.per_month, emi.months) == (992, 48)
    assert emi.provider == "HDFC Bank Debit Card"
    assert emi.kind == "Standard EMI"
    assert emi.provider_count == 20


def test_staging_reports_emi_as_unknown_rather_than_absent():
    """There is no staging twin of the shop's EMI calculator.

    Quoting production EMI against staging prices would be a real instalment
    for a price nobody is charging.
    """
    payload = {**ANSWERED, "summary": {}, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["emi"] = {
        "status": "not_configured",
        "reason": "no emi host for stage: set EMI_HOST to the confirmed one",
    }

    emi = estate_api.read(payload).emi
    assert emi.state == estate_api.UNKNOWN
    assert emi.per_month is None


def test_a_price_the_shop_hides_is_not_spoken():
    payload = {**ANSWERED, "sections": {**ANSWERED["sections"]}}
    payload["sections"]["pricing"] = {
        "status": "ok",
        "data": {"price": 34999, "mrp": 34999, "hide_selling_price": True},
    }

    price = estate_api.read(payload).price
    assert price.state == estate_api.UNKNOWN
    assert price.selling is None


def test_a_response_without_sections_is_an_outage_not_an_answer():
    with pytest.raises(estate_api.EstateUnavailable):
        estate_api.read({"error": "q is required"})
