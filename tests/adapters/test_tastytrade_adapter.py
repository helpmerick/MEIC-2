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
