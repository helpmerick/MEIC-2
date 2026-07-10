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


# --- TPF/TPT (v1.58) monitor-wiring capstone ----------------------------------
# The domain math (domain/tpf.py, domain/tpt.py) and the confirmation-counter
# services (application/tpf_monitor.py, application/tpt_monitor.py) existed for
# a release wired to NOTHING: no monitor object in the live day, no health-tick
# evaluation, no endpoint. An operator-armed floor/target silently did nothing.
# This mirrors the STP-04 close-hook regression
# (test_live_protect_carries_a_real_close_entry_hook_not_none in
# tests/application/test_compositions.py): assert on the object live_app()
# ACTUALLY builds, not on a hand-constructed ExitMonitor a test could keep
# passing even if the real wiring regressed.

def test_live_app_wires_a_real_exit_monitor_not_none(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app
    from meic.application.exit_monitor import ExitMonitor

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()

    assert app.state.exit_monitor is not None
    assert isinstance(app.state.exit_monitor, ExitMonitor)


# --- EC-STP-06 stop-fill catch-up wiring capstone (v1.60) ----------------------
# The FOURTH member of the "exists but unwired" class (after RSK-04's safety
# rails, the ENT-10 day supervisor, and TPF/TPT): reconcile.py already
# implements the EC-STP-06 triage ("a short with no resting stop: did the stop
# fill? -> run LEX") and reconcile_boot.py already drives it -- but only from
# _boot_reconcile() at startup/reconnect. A stop that fills mid-day while the
# bot stays connected (the 2026-07-10 11:56 incident) was never re-checked
# until this: the live health tick now re-runs the SAME frame every ~60s.

def test_live_app_wires_a_real_stop_fill_detector_not_none(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()

    assert app.state.stop_fill_detector is not None
    assert callable(app.state.stop_fill_detector)


def test_stop_fill_detector_drives_lex_with_a_real_quote_from_the_held_snapshot(monkeypatch, tmp_path):
    """2026-07-10 review finding 1 (BLOCKING class): `_long_quote` read
    `ChainSide.marks` with the raw STRING `_strike_from_symbol` returns, but
    marks are keyed by Decimal — so the lookup always missed, the quote guard
    deferred forever, and the catch-up never actually recovered a long in
    production, silently, with every wiring test green. The non-None rail
    capstone above cannot catch that ("looks wired, does nothing"), so this
    drives the REAL wired detector end-to-end: a fake broker holding a
    caught-up stop fill, the held snapshot populated with Decimal-keyed
    marks + spot, and asserts recover() is actually invoked with a REAL
    Quote priced off those marks."""
    import asyncio
    from decimal import Decimal as D
    from types import SimpleNamespace

    from meic.adapters.api.server import live_app
    from meic.application.recover_long import Quote
    from meic.domain.chain import Mark
    from meic.domain.events import CondorFilled, FilledLeg, ShortStopped, StopPlaced

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition

    entry_id = "2026-07-10#1"
    call_short, call_long = "SPXW  260710C07550000", "SPXW  260710C07570000"
    comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("3.32"), legs=(
        FilledLeg(symbol="SPXW  260710P07505000", right="P", role="long", qty=1, price=D("0.10")),
        FilledLeg(symbol="SPXW  260710P07525000", right="P", role="short", qty=1, price=D("1.50")),
        FilledLeg(symbol=call_short, right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=call_long, right="C", role="long", qty=1, price=D("0.08")),
    )))
    comp.events.append(StopPlaced(entry_id=entry_id, side="CALL", trigger=D("3.80"),
                                  broker_order_id="STOP-1"))

    # broker truth: the CALL stop FILLED (caught-up), the long still held
    class _Broker:
        async def working_orders(self):
            return []

        async def fills_since(self, cursor):
            return [{"order_id": "STOP-1", "partial": False}]

        async def fill_legs(self, order_id):
            return (FilledLeg(symbol=call_short, right="C", role="short", qty=1, price=D("3.85")),)

        async def positions(self):
            return [{"symbol": call_long, "signed_qty": 1}]

    comp.broker = _Broker()

    # the held snapshot the REAL `_long_quote` reads: Decimal-keyed marks + spot
    snaps = app.state.chain_snapshots
    snaps.stale = False
    snaps.last = SimpleNamespace(
        stale=False, spot=D("7500"),
        put_side=SimpleNamespace(marks={}),
        call_side=SimpleNamespace(marks={D("7570"): Mark(bid=D("0.35"), ask=D("0.45"))}))

    recovered: list[dict] = []

    class _RecoverSpy:
        async def recover(self, **kw):
            recovered.append(kw)

    comp.recover = _RecoverSpy()

    asyncio.run(app.state.stop_fill_detector())

    assert any(isinstance(e, ShortStopped) for e in comp.events), "the caught-up fill was journaled"
    assert len(recovered) == 1, "LEX must actually be DRIVEN off the held snapshot's marks"
    call = recovered[0]
    assert call["entry_id"] == entry_id and call["side"] == "CALL"
    assert call["long_symbol"] == call_long
    assert isinstance(call["quote"], Quote)
    assert call["quote"].bid == D("0.35") and call["quote"].ask == D("0.45")
    assert call["intrinsic"] == D("0")   # spot 7500 vs 7570 call: OTM


def test_live_app_commands_carry_a_real_tpf_tpt_gap_provider(monkeypatch, tmp_path):
    """TPF-02/TPT-03 server-side gap validation needs a real profit% source —
    a `None` provider means every set/raise/lower request is silently
    rejected as 'unavailable' forever, which is indistinguishable from a
    wiring bug. The provider live_app() builds must be callable and read off
    the SAME live composition, not a stub."""
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    commands = app.state.commands
    assert commands._profit_pct_provider is not None
    # no chain snapshot yet (never connected in this offline test) -> honest
    # "unknown", never a crash and never a fabricated number.
    assert commands._profit_pct_provider("2026-07-08#1") is None


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


def test_chain_atm_band_pts_is_retired_from_server_wiring():
    """STK-10 v1.51: `chain_atm_band_pts` (and its `_chain_band_pts` reader) is
    RETIRED — a fixed ATM band can't track the moving far-OTM dead-strike
    boundary. STK-10 now gates on the entry's own trade-relative reachable set
    (domain/chain.py: `reachable_strikes`), so server.py must no longer offer
    any way to configure a band at all."""
    import meic.adapters.api.server as server_module

    assert not hasattr(server_module, "_chain_band_pts")


def test_entry_window_seconds_is_read_from_config_not_hardcoded():
    """STK-10 v1.51 / ENT-02 (doc 06: range 10-600, default 120) — bounds how
    long the selector's retry loop may keep taking fresh snapshots."""
    from meic.adapters.api.server import _entry_window_seconds

    assert _entry_window_seconds({}) == 120                                       # spec default
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "60"}) == 60        # operator value
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "10"}) == 10        # low edge
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "600"}) == 600      # high edge
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "5"}) == 120        # < 10 -> default
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "9999"}) == 120     # > 600 -> default
    assert _entry_window_seconds({"MEIC_ENTRY_WINDOW_SECONDS": "junk"}) == 120


def test_chain_retry_seconds_is_read_from_config_not_hardcoded():
    """STK-10 v1.51 `chain_retry_seconds` (doc 06: range 1-30, default 5) — the
    interval between fresh-snapshot retries while the gate is unhealed."""
    from meic.adapters.api.server import _chain_retry_seconds

    assert _chain_retry_seconds({}) == 5                                        # spec default
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "10"}) == 10        # operator value
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "1"}) == 1          # low edge
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "30"}) == 30        # high edge
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "0"}) == 5          # < 1 -> default
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "31"}) == 5         # > 30 -> default
    assert _chain_retry_seconds({"MEIC_CHAIN_RETRY_SECONDS": "junk"}) == 5


# --- REC-01 / REC-07(8): the live event log must be DURABLE ---------------------

def test_live_app_event_log_is_durable_and_survives_a_rebuild(monkeypatch, tmp_path):
    """Today's gap (v1.54 slice 1): live_app's event log used to be the plain
    in-memory `EventLog`, so a process restart lost the whole log. An event
    appended in one process must be visible after "restarting" — building a
    fresh live_app() over the SAME MEIC_DATA_DIR/state.db — and still show up
    through the ordinary read path (/entries), not just in comp.events."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.application.event_log import DurableEventLog
    from meic.domain.events import CondorFilled, DayArmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app1 = live_app()
    comp1 = app1.state.composition
    assert isinstance(comp1.events, DurableEventLog)

    comp1.events.append(DayArmed(date="2026-07-09", entry_count=1))
    comp1.events.append(CondorFilled(entry_id="2026-07-09#1", net_credit=Decimal("4.00")))

    app2 = live_app()  # a fresh composition over the SAME db file
    comp2 = app2.state.composition
    assert [type(e).__name__ for e in comp2.events] == ["DayArmed", "CondorFilled"]
    assert comp2.events[1].net_credit == Decimal("4.00")

    cards = TestClient(app2).get("/entries").json()
    assert any(c["entry_id"] == "2026-07-09#1" and c["net_credit"] == "4.00" for c in cards)
