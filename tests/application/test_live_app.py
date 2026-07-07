"""live_app() wiring — the real composition behind the panel, offline (no
network: construction is I/O-free; connect() only runs on app startup)."""
import base64
import json

import pytest

from meic.adapters.persistence.event_store import SqliteStateStore
from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.persistent_state import PersistentState


def _jwt(iss: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _cert_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "s")
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", _jwt("https://api.sandbox.tastyworks.com"))
    monkeypatch.setenv("TT_CERT_ACCOUNT", "5WZ00000")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))


def test_live_app_requires_api_token(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    _cert_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MEIC_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MEIC_API_TOKEN"):  # NFR-06
        live_app()


def test_live_app_wires_live_adapter_with_safe_defaults_and_persistence(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_API_TOKEN", "panel-secret")

    app = live_app()
    comp = app.state.composition

    # bound to the REAL adapter, never the simulator (EC-RSK-04)
    assert isinstance(comp.broker, TastytradeAdapter)
    assert not isinstance(comp.broker, SimulatedBroker)
    assert comp.state.trading_mode == "live"

    # SAFE DEFAULTS: nothing trades until the operator deliberately acts
    assert comp.state.armed is False
    assert comp.state.confirm_live is False
    assert comp.state.entries_enabled() is False

    # durable state (REC-07): SQLite-backed, survives a "restart"
    assert (tmp_path / "state.db").exists()
    comp.state.armed = True
    reopened = PersistentState(SqliteStateStore(tmp_path / "state.db"))
    assert reopened.armed is True and reopened.trading_mode == "live"


def test_live_app_refuses_missing_credentials(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    monkeypatch.setenv("MEIC_API_TOKEN", "panel-secret")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "")   # explicitly blank overrides .env
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", "")
    with pytest.raises(RuntimeError, match="CERT broker credentials"):
        live_app()
