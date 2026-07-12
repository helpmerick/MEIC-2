"""TastytradeAdapter — offline-testable guards (issuer, Day-TIF, allocation).

The live BrokerGateway calls are proven by contract tests (pytest -m contract);
these assert the assumption-enforcing guards that need no network.
"""
from decimal import Decimal as D

import pytest

from meic.adapters.tastytrade.adapter import NonCertTokenRefused, TastytradeAdapter, assert_cert_token
from meic.domain.allocation import AllocationGate

# a cert-issuer JWT (iss = api.sandbox.tastyworks.com) — header.payload.sig, sig ignored
import base64
import json


def _jwt(iss: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss, 'scope': 'read trade'})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")
PROD = _jwt("https://api.tastytrade.com")


def test_assumption10_refuses_production_token_before_any_network_call():
    with pytest.raises(NonCertTokenRefused):
        assert_cert_token(PROD)
    assert_cert_token(CERT)  # cert issuer accepted


def test_test_wiring_refuses_production_token_at_construction():
    with pytest.raises(NonCertTokenRefused):
        TastytradeAdapter("secret", PROD, is_test=True)
    TastytradeAdapter("secret", CERT, is_test=True)  # ok — no network yet


def test_assumption2_option_stop_must_be_day_tif():
    """Defence in depth: OrderIntent refuses a GTC option stop at construction,
    and the adapter re-checks an intent that bypassed the constructor."""
    from datetime import date

    from meic.application.order_intent import IntentError, OrderIntent, OrderLeg

    exp = date(2026, 7, 8)
    put = OrderLeg(right="P", action="buy_to_close", qty=1, strike=D("5990"))

    # 1) the canonical type makes a GTC option stop unconstructable
    with pytest.raises(IntentError, match="Day-TIF"):
        OrderIntent(order_type="stop_market", tif="GTC", contracts=1, expiration=exp,
                    stop_trigger=D("3.80"), legs=(put,))

    # 2) the adapter still refuses one that bypassed the constructor
    smuggled = object.__new__(OrderIntent)
    object.__setattr__(smuggled, "order_type", "stop_market")
    object.__setattr__(smuggled, "tif", "GTC")
    with pytest.raises(ValueError, match="Day only"):
        TastytradeAdapter.validate_stop_tif(smuggled)

    # 3) a Day stop and a non-stop pass
    TastytradeAdapter.validate_stop_tif(
        OrderIntent(order_type="stop_market", tif="Day", contracts=1, expiration=exp,
                    stop_trigger=D("3.80"), legs=(put,)))
    TastytradeAdapter.validate_stop_tif(
        OrderIntent(order_type="limit", tif="Day", contracts=1, expiration=exp,
                    price=D("1.00"), legs=(put,)))


def test_assumption5_records_allocation_on_real_fill():
    gate = AllocationGate()
    adapter = TastytradeAdapter("secret", CERT, is_test=True, allocation_gate=gate, tick=D("0.05"))
    rec = adapter.record_fill_allocation([D("1.35"), D("-0.15"), D("1.25"), D("-0.15")], net_fill=D("2.30"))
    assert rec.passed is True and gate.consecutive_passed == 1
    bad = adapter.record_fill_allocation([D("0.50"), D("0.00")], net_fill=D("0.05"))
    assert bad.passed is False and gate.consecutive_passed == 0  # FAIL resets


def test_server_time_parses_the_broker_date_header():
    """DAY-03 (v1.48): drift is measured against the broker's `Date` header on the
    session probe. Offline — the response is injected, no session."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace

    adapter = TastytradeAdapter("secret", CERT, is_test=True)

    async def resp():   # an httpx-like response carrying a Date header
        return SimpleNamespace(headers={"date": "Wed, 08 Jul 2026 15:00:00 GMT"})
    adapter._probe_response = resp

    got = asyncio.run(adapter.server_time())
    assert got == datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)


def test_server_time_is_none_when_the_header_is_missing_or_the_probe_fails():
    """None = 'no reading', which the clock probe treats as unverified -> blocked.
    A probe that cannot read the header must never crash the health loop."""
    import asyncio
    from types import SimpleNamespace

    adapter = TastytradeAdapter("secret", CERT, is_test=True)

    async def no_header():
        return SimpleNamespace(headers={})
    adapter._probe_response = no_header
    assert asyncio.run(adapter.server_time()) is None

    async def boom():
        raise RuntimeError("network down")
    adapter._probe_response = boom
    assert asyncio.run(adapter.server_time()) is None   # degrades to blocked, never raises


def test_probe_response_uses_the_sdk_client_and_validate_endpoint():
    """Regression: the real probe must reach the SDK's httpx client (`_client` in
    v13) and POST `/sessions/validate` — the attribute/method the SDK actually
    exposes. The prior `async_client`/GET pair silently returned None, so the
    DAY-03 clock never verified and arming was blocked forever."""
    import asyncio
    from types import SimpleNamespace

    calls = []

    class _Client:
        async def post(self, path):
            calls.append(("post", path))
            return SimpleNamespace(headers={"date": "Wed, 08 Jul 2026 15:00:00 GMT"})

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._session = SimpleNamespace(_client=_Client())   # v13 attribute name

    resp = asyncio.run(adapter._probe_response())
    assert calls == [("post", "/sessions/validate")]
    assert resp.headers["date"].endswith("GMT")


# --- RPT-15 read-only fetches (day_fills / cash_and_fees) --------------------

def test_day_fills_calls_get_history_scoped_to_the_day():
    import asyncio
    from datetime import date

    calls = []

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            calls.append((start_date, end_date, type))
            return ["fill1", "fill2"]

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    result = asyncio.run(adapter.day_fills("2026-07-09"))
    assert result == ["fill1", "fill2"]
    assert calls == [(date(2026, 7, 9), date(2026, 7, 9), "Trade")]


def test_day_settlements_calls_get_history_for_receive_deliver_through_next_day():
    """RPT-16 settlement import (operator ruling 2026-07-10): same
    `get_history` GET surface as day_fills, `type="Receive Deliver"` filtered
    server-side (the SDK's `type` param is an exact transaction_type match),
    and `end_date = day + 1` because a settlement can post to the broker's
    ledger the day AFTER the trading day it settles."""
    import asyncio
    from datetime import date

    calls = []

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            calls.append((start_date, end_date, type))
            return ["settle1"]

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    result = asyncio.run(adapter.day_settlements("2026-07-09"))
    assert result == ["settle1"]
    assert calls == [(date(2026, 7, 9), date(2026, 7, 10), "Receive Deliver")]


def test_cash_and_fees_sums_net_value_and_reads_total_fees():
    """EOD-01 v1.59: the fake discriminates by `type` (as the real SDK's
    server-side filter does) -- Trade rows only here, no settlements posted
    this day, so cash_delta is exactly the Trade-side sum, unchanged."""
    import asyncio
    from datetime import date
    from types import SimpleNamespace

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            if type == "Trade":
                return [SimpleNamespace(net_value=D("100.00")), SimpleNamespace(net_value=D("-20.50"))]
            return []  # "Receive Deliver" -- nothing settled this day

        async def get_total_fees(self, session, *, day):
            assert day == date(2026, 7, 9)
            return SimpleNamespace(total_fees=D("2.40"))

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    cash_delta, fees = asyncio.run(adapter.cash_and_fees("2026-07-09"))
    assert cash_delta == D("79.50")
    assert fees == D("2.40")


def test_cash_and_fees_v1_59_includes_settlement_net_value():
    """EOD-01 v1.59: a trades-only sum silently misses an ITM-expiring
    short's real cash effect -- the pinned 2026-07-09 vector: +355.12 in
    Trade fills alone, vs the true -13.88 once the -369.00 Receive-Deliver
    settlement is folded in."""
    import asyncio
    from types import SimpleNamespace

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            if type == "Trade":
                return [SimpleNamespace(net_value=D("355.12"))]
            return [SimpleNamespace(net_value=D("-369.00"))]  # Receive Deliver

        async def get_total_fees(self, session, *, day):
            return SimpleNamespace(total_fees=D("9.88"))

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    cash_delta, fees = asyncio.run(adapter.cash_and_fees("2026-07-09"))
    assert cash_delta == D("-13.88")
    assert fees == D("9.88")


def test_cash_and_fees_ignores_a_settlement_row_with_no_net_value():
    """A settlement row missing net_value contributes nothing, never a
    fabricated 0 folded in as if it were real."""
    import asyncio
    from types import SimpleNamespace

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            if type == "Trade":
                return [SimpleNamespace(net_value=D("100.00"))]
            return [SimpleNamespace(net_value=None)]

        async def get_total_fees(self, session, *, day):
            return SimpleNamespace(total_fees=D("0"))

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    cash_delta, _ = asyncio.run(adapter.cash_and_fees("2026-07-09"))
    assert cash_delta == D("100.00")


def test_cash_and_fees_on_a_day_with_no_fills_is_zero_delta():
    import asyncio

    class _FakeAccount:
        async def get_history(self, session, *, start_date, end_date, type):
            return []

        async def get_total_fees(self, session, *, day):
            from types import SimpleNamespace
            return SimpleNamespace(total_fees=D("0"))

    adapter = TastytradeAdapter("secret", CERT, is_test=True)
    adapter._account = _FakeAccount()

    cash_delta, fees = asyncio.run(adapter.cash_and_fees("2026-07-09"))
    assert cash_delta == D("0") and fees == D("0")
