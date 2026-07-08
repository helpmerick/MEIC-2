"""Symmetric environment guards: the cert wiring refuses production tokens, and
the production wiring refuses cert tokens — both BEFORE any network call. Plus
the two-switch opt-in that stands between a config typo and real money."""
import base64
import json

import pytest

from meic.adapters.tastytrade.adapter import (
    NonCertTokenRefused,
    NonProductionTokenRefused,
    TastytradeAdapter,
    assert_cert_token,
    assert_production_token,
)

CERT = "https://api.sandbox.tastyworks.com"
PROD = "https://api.tastytrade.com"


def _jwt(iss: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


# --- the two guards, in both directions --------------------------------------

def test_cert_guard_accepts_cert_refuses_production():
    assert_cert_token(_jwt(CERT))  # no raise
    with pytest.raises(NonCertTokenRefused):
        assert_cert_token(_jwt(PROD))
    with pytest.raises(NonCertTokenRefused):
        assert_cert_token("not-a-jwt")


def test_production_guard_accepts_production_refuses_cert():
    assert_production_token(_jwt(PROD))  # no raise
    with pytest.raises(NonProductionTokenRefused, match="CERT/SANDBOX"):
        assert_production_token(_jwt(CERT))  # a cert token in the prod slot
    with pytest.raises(NonProductionTokenRefused, match="no decodable issuer"):
        assert_production_token("not-a-jwt")


# --- the adapter applies the right guard for its environment ------------------

def test_adapter_refuses_production_token_in_cert_wiring():
    with pytest.raises(NonCertTokenRefused):
        TastytradeAdapter("secret", _jwt(PROD), is_test=True)


def test_adapter_refuses_cert_token_in_production_wiring():
    """The mirror guard: a cert token slotted into TT_PROD_* fails loudly here,
    not confusingly at auth time."""
    with pytest.raises(NonProductionTokenRefused):
        TastytradeAdapter("secret", _jwt(CERT), is_test=False)


def test_adapter_accepts_matching_tokens_construction_is_io_free():
    assert TastytradeAdapter("secret", _jwt(CERT), is_test=True) is not None
    assert TastytradeAdapter("secret", _jwt(PROD), is_test=False) is not None


# --- live_app: two deliberate switches, never one ----------------------------

def _prod_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEIC_API_TOKEN", "panel-secret")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "false")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TT_PROD_PROVIDER_SECRET", "s")
    monkeypatch.setenv("TT_PROD_REFRESH_TOKEN", _jwt(PROD))


def test_live_app_refuses_production_without_the_second_opt_in(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    _prod_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MEIC_ALLOW_PRODUCTION", raising=False)
    with pytest.raises(RuntimeError, match="REFUSING to wire PRODUCTION"):
        live_app()
    monkeypatch.setenv("MEIC_ALLOW_PRODUCTION", "yes")  # wrong phrase
    with pytest.raises(RuntimeError, match="REFUSING to wire PRODUCTION"):
        live_app()


def test_live_app_wires_production_only_with_both_switches(monkeypatch, tmp_path):
    from meic.adapters.api.server import PRODUCTION_OPT_IN, live_app
    _prod_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_ALLOW_PRODUCTION", PRODUCTION_OPT_IN)

    app = live_app()  # construction is I/O-free; no network, no orders
    comp = app.state.composition
    assert comp.broker._is_test is False   # production wiring
    assert comp.state.trading_mode == "live"
    assert comp.state.entries_enabled() is False  # still DISARMED + Confirm Live OFF


def test_live_app_production_rejects_a_cert_token_in_the_prod_slot(monkeypatch, tmp_path):
    from meic.adapters.api.server import PRODUCTION_OPT_IN, live_app
    _prod_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_ALLOW_PRODUCTION", PRODUCTION_OPT_IN)
    monkeypatch.setenv("TT_PROD_REFRESH_TOKEN", _jwt(CERT))  # wrong environment
    with pytest.raises(NonProductionTokenRefused):
        live_app()
