"""The readiness check, and the one line in it that must never be wrong.

`make estate` is read by a person deciding whether live stock is working. If
it renders "could not ask" as "out of stock" it tells that person the opposite
of the truth, in the same words the agent would use to a customer.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.tools import estate
from src.tools.check_estate import _emi, _price, _stock, _wrap, live, readiness, safe


# Shaped like the real ones and deliberately not them. A test that proves a
# redactor works by pasting a live credential into the repository has proved
# the opposite of what it set out to.
#
# The pragmas are for detect-secrets, which is right to stop at all three -
# they are high-entropy strings in a committed file, which is exactly what a
# leaked token looks like. Marked inline rather than parked in
# .secrets.baseline so the judgement sits next to the value: the head
# decodes to {"alg":"none","typ":"JWT"} and the body to
# {"username":"not-a-real-token"}, which anyone reviewing this can check.
FAKE_ACCESS_TOKEN = "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQq"  # noqa: S105  # pragma: allowlist secret
FAKE_JWT_HEAD = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"  # pragma: allowlist secret
FAKE_JWT_BODY = "eyJ1c2VybmFtZSI6Im5vdC1hLXJlYWwtdG9rZW4ifQ"  # pragma: allowlist secret


class TestRedaction:
    def test_a_token_does_not_survive(self):
        assert FAKE_ACCESS_TOKEN not in safe(
            f"access-token {FAKE_ACCESS_TOKEN} refused"
        )

    def test_a_hostname_does(self):
        """Dots are not redacted, so a host stays readable.

        `describe` already cuts a host down to its name - the PIM and offers
        hosts carry a token in the path - so this is the second of two
        guards, not the only one.
        """
        assert safe("ci-gateway.poorvika.com") == "ci-gateway.poorvika.com"

    def test_a_jwt_goes(self):
        jwt = f"{FAKE_JWT_HEAD}.{FAKE_JWT_BODY}"
        assert FAKE_JWT_HEAD not in safe(f"Bearer {jwt}")
        assert FAKE_JWT_BODY not in safe(f"Bearer {jwt}")


class TestHowStockReads:
    def test_unknown_says_unknown_and_never_says_none(self):
        """The distinction the estate module exists for.

        "We could not ask" and "there are none" are different facts, and only
        one of them is safe to act on. A phone on a shelf described as out of
        stock is the error a customer can see.
        """
        line = _stock(estate.Stock(estate.UNKNOWN, reason="CI_HOST is not set"))
        assert "UNKNOWN" in line
        assert "out of stock" not in line
        assert "CI_HOST is not set" in line

    def test_a_real_zero_says_out_of_stock(self):
        line = _stock(estate.Stock(estate.KNOWN, in_stock=False, quantity=0))
        assert line == "known, out of stock"

    def test_stock_on_a_shelf_says_where(self):
        line = _stock(
            estate.Stock(estate.KNOWN, in_stock=True, quantity=34, branches=24)
        )
        assert "in stock" in line
        assert "34" in line and "24" in line

    def test_the_none_state_is_also_a_real_zero(self):
        assert _stock(estate.Stock(estate.NONE)) == "known, out of stock"


class TestTheOtherLines:
    def test_a_price_that_answered(self):
        assert _price(estate.Price(estate.KNOWN, selling=69990)) == "known, 69,990"

    def test_a_price_that_did_not(self):
        line = _price(estate.Price(estate.UNKNOWN, reason="not for display"))
        assert "unknown" in line and "not for display" in line

    def test_an_emi_that_answered(self):
        line = _emi(estate.Emi(estate.KNOWN, per_month=1984, months=48))
        assert "1,984/month over 48" in line

    def test_an_emi_with_no_host(self):
        assert "no emi host" in _emi(estate.Emi(estate.UNKNOWN, reason="no emi host"))


class TestWrapping:
    def test_a_long_problem_is_wrapped_not_cut(self):
        """The problem strings name the variable to set; losing the tail
        would turn a usable instruction into "set CI_HOST to the"."""
        problem = (
            "no ci host for production: set CI_HOST to the confirmed one - ask "
            "for a production host and a token issued for the mode it is sent with"
        )
        lines = _wrap(problem, 40)
        assert all(len(line) <= 40 for line in lines)
        assert " ".join(lines) == problem


class FakeBackend:
    def __init__(self, described: dict[str, Any], answers: dict[str, Any]) -> None:
        self._described = described
        self._answers = answers

    def describe(self) -> dict[str, Any]:
        return self._described

    async def product(self, item_code: str, sections: tuple[str, ...]) -> Any:
        answer = self._answers[item_code]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeSettings:
    estate_backend = "inprocess"
    estate_api_env = "production"
    estate_secrets = {"CI_AUTH_TOKEN": "x", "CI_HOST": "", "PIM_ACCESS_TOKEN": "y"}


READY = {
    "api_env": "production",
    "services": {
        "ci": {"ready": True, "host": "ci-gateway.poorvika.com"},
        "pim": {"ready": True, "host": "pim-gateway.poorvika.com"},
    },
}
NOT_READY = {
    "api_env": "production",
    "services": {
        "ci": {"ready": False, "host": None, "problem": "no ci host for production"},
        "pim": {"ready": True, "host": "pim-gateway.poorvika.com"},
    },
}


class TestTheExitCode:
    def test_everything_ready_counts_nothing(self, capsys):
        assert readiness(FakeSettings(), FakeBackend(READY, {})) == 0
        assert "ci        ready" in capsys.readouterr().out

    def test_a_service_that_is_not_ready_is_counted_and_explained(self, capsys):
        """Non-zero, so a deploy can use this as a gate."""
        assert readiness(FakeSettings(), FakeBackend(NOT_READY, {})) == 1
        printed = capsys.readouterr().out
        assert "NOT READY" in printed
        assert "no ci host for production" in printed

    def test_a_secret_is_never_printed_by_the_readiness_half(self, capsys):
        readiness(FakeSettings(), FakeBackend(READY, {}))
        printed = capsys.readouterr().out
        # The names are listed; the values are not.
        assert "CI_AUTH_TOKEN" in printed
        assert "CI_HOST" in printed  # listed under "not set"
        assert "PIM_ACCESS_TOKEN" in printed


# The shape of a production answer, keyed the way the Worker keys it:
# `branch_count` not `branches`, and the EMI headline off the summary.
ANSWERED = {
    "took_ms": 491,
    "sections": {
        "pricing": {"status": "ok", "data": {"selling_price": 69990}},
        "stock": {
            "status": "ok",
            "data": {"quantity": 34, "in_stock": True, "branch_count": 24},
        },
        "offers": {"status": "ok", "data": {"offers": []}},
        "emi": {"status": "ok", "data": {"provider_count": 20}},
    },
    "summary": {
        "selling_price": 69990,
        "emi_starts_from": {"per_month": 1984, "months": 48, "provider": "HDFC"},
    },
}


class TestTheLiveHalf:
    async def test_a_section_that_could_not_be_asked_is_counted(self, capsys):
        """The case the whole tool was written for.

        Readiness said yes and the call still did not answer - which is what
        a bare hostname does, and what a refused token does with HTTP 200.
        """
        unanswerable = {
            "took_ms": 1008,
            "sections": {
                "pricing": {"status": "ok", "data": {"selling_price": 69990}},
                "stock": {"status": "unavailable", "reason": "missing protocol"},
            },
            "summary": {"selling_price": 69990},
        }
        backend = FakeBackend(READY, {"54486": unanswerable})
        assert await live(backend, ("54486",)) == 1
        printed = capsys.readouterr().out
        assert "UNKNOWN" in printed
        assert "could not ask: stock" in printed

    async def test_everything_answered_counts_nothing(self, capsys):
        backend = FakeBackend(READY, {"54486": ANSWERED})
        assert await live(backend, ("54486",)) == 0
        assert "could not ask" not in capsys.readouterr().out

    async def test_a_lookup_that_failed_entirely_is_not_a_result(self, capsys):
        backend = FakeBackend(READY, {"54486": estate.EstateUnavailable("no network")})
        assert await live(backend, ("54486",)) == 1
        assert "the whole lookup failed" in capsys.readouterr().out


@pytest.mark.parametrize("state", [estate.UNKNOWN, estate.NONE, estate.KNOWN])
def test_every_state_renders_without_a_reason(state):
    """None of the three may crash on a Stock carrying no reason at all."""
    assert _stock(estate.Stock(state))
