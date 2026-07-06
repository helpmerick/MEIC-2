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
    with pytest.raises(ValueError):
        TastytradeAdapter.validate_stop_tif({"type": "stop_market", "tif": "GTC"})
    TastytradeAdapter.validate_stop_tif({"type": "stop_market", "tif": "Day"})  # ok
    TastytradeAdapter.validate_stop_tif({"type": "limit", "tif": "GTC"})  # non-stop unaffected


def test_assumption5_records_allocation_on_real_fill():
    gate = AllocationGate()
    adapter = TastytradeAdapter("secret", CERT, is_test=True, allocation_gate=gate, tick=D("0.05"))
    rec = adapter.record_fill_allocation([D("1.35"), D("-0.15"), D("1.25"), D("-0.15")], net_fill=D("2.30"))
    assert rec.passed is True and gate.consecutive_passed == 1
    bad = adapter.record_fill_allocation([D("0.50"), D("0.00")], net_fill=D("0.05"))
    assert bad.passed is False and gate.consecutive_passed == 0  # FAIL resets
