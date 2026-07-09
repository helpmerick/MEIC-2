"""live_app() wiring — the real composition behind the panel, offline (no
network: construction is I/O-free; connect() only runs on app startup)."""
import base64
import json

from decimal import Decimal

import pytest

from meic.adapters.persistence.event_store import SqliteStateStore
from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.persistent_state import PersistentState


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Tests must NOT read the operator's real gitignored .env -- a populated .env
    (production creds, User Password) would mask the absence these tests assert.
    Isolate live_app's env to os.environ, which monkeypatch fully controls."""
    import os as _os
    from meic.adapters.api import server
    monkeypatch.setattr(server, "_read_env", lambda: dict(_os.environ))


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
    monkeypatch.delenv("MEIC_USER_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="MEIC_USER_PASSWORD"):  # NFR-06
        live_app()


def test_live_app_wires_live_adapter_with_safe_defaults_and_persistence(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition

    # bound to the REAL adapter, never the simulator (EC-RSK-04). It is wrapped in
    # a CountingBroker so the RSK-08 order cap sees every order any service submits.
    from meic.composition.live_wiring import CountingBroker

    assert isinstance(comp.broker, CountingBroker)
    assert isinstance(comp.broker.inner, TastytradeAdapter)
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
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "")   # explicitly blank overrides .env
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", "")
    with pytest.raises(RuntimeError, match="CERT broker credentials"):
        live_app()


# --- the live-wiring capstone, at the live_app() level --------------------------
# This test already existed and did NOT catch the missing rails, because it only
# checked which BROKER was bound. Whether the SAFETY RAILS are armed is the thing
# that decides if the bot can lose money it was told not to.

SAFETY_RAILS = ("max_day_risk", "order_cap", "buying_power")


def _compose_schedule(tmp_path, *, max_day_risk="20000", rows=None):
    """Write a saved schedule into the SAME durable store live_app() will open."""
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.persistent_state import PersistentState
    from meic.application.schedule_service import ScheduleService

    state = PersistentState(SqliteStateStore(tmp_path / "state.db"))
    out = ScheduleService(state).save(rows or [{"time": "10:00", "contracts": 2}],
                                      max_day_risk=max_day_risk)
    assert out["result"] == "saved", out


def test_live_app_arms_every_safety_rail(monkeypatch, tmp_path):
    """RSK-04, RSK-08 and the ENT-03 buying-power gate, on the object live_app
    actually builds. Before v1.47 all three were None here while paper had them."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    _compose_schedule(tmp_path)                       # the operator saved a ceiling

    runtime = live_app().state.runtime

    for rail in SAFETY_RAILS:
        assert getattr(runtime, rail) is not None, f"{rail} is not armed in live_app()"
    assert runtime.max_day_risk == Decimal("20000")   # read from the panel, not a default


def test_an_unconfigured_ceiling_stays_none_and_uc02_refuses_the_live_arm(monkeypatch, tmp_path):
    """doc 06 §169. `None` is 'no ceiling configured' — never 'unlimited'. The rail
    is wired; it simply has nothing to enforce until the operator sets one, and the
    pre-flight is what stops a live arm in that state."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    runtime = live_app().state.runtime                # nothing composed at all
    assert runtime.max_day_risk is None
    assert runtime.order_cap is not None and runtime.buying_power is not None


def test_live_app_carries_each_rows_own_contracts_into_the_day(monkeypatch, tmp_path):
    """ENT-04. The live day used to keep only the times, so a 2-contract row traded
    1 contract at the global premium/width/stop."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    _compose_schedule(tmp_path, rows=[{"time": "10:00", "contracts": 2, "wing_width": "30"},
                                      {"time": "11:15", "contracts": 1}])

    app = live_app()
    rows = app.state.todays_rows()

    assert [r.entry.contracts for r in rows] == [2, 1]
    assert rows[0].selection.wing_width == Decimal("30")
    assert rows[0].stop is not None


def test_live_app_wires_the_manual_fire_and_a_real_preflight(monkeypatch, tmp_path):
    """ENT-09 + UC-02. An unwired ▶ is inert; a stubbed pre-flight ticks green and
    tells the operator nothing."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    _compose_schedule(tmp_path)

    app = live_app()
    commands = app.state.commands

    assert commands._manual is not None                       # the ▶ is real
    assert set(commands.preflight_checks) == {"reconcile", "clock", "config", "market_data"}
    # the manual fire and the scheduled day share ONE exposure book (RSK-04)
    assert app.state.runtime._worst_case is app.state.composition.worst_case


def test_live_app_blocks_trading_until_the_first_clock_probe_lands(monkeypatch, tmp_path):
    """DAY-03 (v1.48): drift is measured against the broker's Date header on the
    ~60s session probe. At boot no probe has landed, so the clock is UNVERIFIED ->
    infinite drift -> every entry skips `clock_drift`. Never assumed to be zero."""
    from meic.application.entry_gates import clock_drift_blocks_entry
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    runtime = live_app().state.runtime
    assert runtime.measure_drift_ms() == float("inf")     # nothing probed yet
    assert clock_drift_blocks_entry(drift_ms=runtime.measure_drift_ms(),
                                    max_drift_ms=runtime.max_clock_drift_ms) is True


def test_the_clock_rail_is_fed_by_the_session_probe_not_an_env_var(monkeypatch, tmp_path):
    """DAY-03 (v1.48): drift is measured against the broker Date header on the
    ~60s session probe. There is no MEIC_CLOCK_DRIFT_MS env path any more."""
    import asyncio
    from datetime import datetime, timezone
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_CLOCK_DRIFT_MS", "42")   # set, and deliberately IGNORED

    app = live_app()
    runtime = app.state.runtime
    assert runtime.measure_drift_ms() == float("inf")   # nothing probed yet -> blocked

    # the session-probe gate is what records a reading. Drive it against a broker
    # whose clock matches ours, and the rail clears.
    gate = app.state.session_probe
    comp = app.state.composition
    now = datetime.now(timezone.utc)
    comp.broker._inner.working_orders = lambda: _async([])   # not connected in a unit test
    comp.broker._inner.server_time = lambda: _async(now)     # broker clock == ours
    asyncio.run(gate())
    assert abs(runtime.measure_drift_ms()) < 2000.0       # verified, inside tolerance


def test_live_app_verifies_the_clock_on_boot_so_the_operator_can_arm(monkeypatch, tmp_path):
    """DAY-03 regression: the live app must actually RUN the session/clock probe on
    startup, not merely expose it. Before this, live_app wired `session_probe` but
    nothing called it on a timer, so the clock was never verified and the arm
    pre-flight blocked forever. Startup must take one reading."""
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition
    runtime = app.state.runtime
    now = datetime.now(timezone.utc)

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect
    comp.broker._inner.positions = lambda: _async([])      # empty book -> reconcile clean
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(now)   # broker clock == ours

    assert runtime.measure_drift_ms() == float("inf")      # nothing probed yet
    with TestClient(app):                                  # runs startup: connect -> probe
        assert abs(runtime.measure_drift_ms()) < 2000.0    # clock verified on boot


async def _async(v):
    return v


def test_chain_atm_band_pts_is_read_from_config_not_hardcoded():
    """STK-10 regression: chain_atm_band_pts (doc 06) must be wired, so the operator
    can narrow the completeness band to where quotes exist. Far-OTM 0DTE strikes are
    listed but unquoted; a hardcoded wide band fails selection on strikes that will
    never have a mark. Out-of-range falls back to the spec default (150)."""
    from decimal import Decimal
    from meic.adapters.api.server import _chain_band_pts

    assert _chain_band_pts({}) == Decimal("150")                                # spec default
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "70"}) == Decimal("70")  # operator value
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "50"}) == Decimal("50")  # low edge
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "500"}) == Decimal("500")
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "10"}) == Decimal("150")  # < 50 -> default
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "999"}) == Decimal("150") # > 500 -> default
    assert _chain_band_pts({"MEIC_CHAIN_ATM_BAND_PTS": "junk"}) == Decimal("150")
