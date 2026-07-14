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


def test_stop_fill_poll_seconds_is_read_from_config_not_hardcoded():
    """ITEM 1 follow-up (operator ruling 2026-07-11) `MEIC_STOP_FILL_POLL_S`
    (range 5-120, default 15) — the dedicated stop-fill fallback poll loop's
    cadence, same reject-the-dial convention as every other reader in this
    file (e.g. `_entry_window_seconds`/`_chain_retry_seconds` above)."""
    from meic.adapters.api.server import _stop_fill_poll_seconds

    assert _stop_fill_poll_seconds({}) == 15.0                                       # default
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "20"}) == 20.0           # operator value
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "5"}) == 5.0             # low edge
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "120"}) == 120.0         # high edge
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "4.99"}) == 15.0         # < 5 -> default
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "121"}) == 15.0          # > 120 -> default
    assert _stop_fill_poll_seconds({"MEIC_STOP_FILL_POLL_S": "junk"}) == 15.0


def test_watchdog_grace_seconds_is_read_from_config_not_hardcoded():
    """STP-03b `watchdog_grace_seconds` (doc 06: range 3-60, default 10)."""
    from decimal import Decimal as D

    from meic.adapters.api.server import _watchdog_grace_seconds

    assert _watchdog_grace_seconds({}) == D("10")                                        # default
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "15"}) == D("15")      # operator value
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "3"}) == D("3")        # low edge
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "60"}) == D("60")      # high edge
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "2"}) == D("10")       # < 3 -> default
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "61"}) == D("10")      # > 60 -> default
    assert _watchdog_grace_seconds({"MEIC_WATCHDOG_GRACE_SECONDS": "junk"}) == D("10")


def test_watchdog_escalate_seconds_is_read_from_config_not_hardcoded():
    """STP-03b `watchdog_escalate_seconds` (doc 06: range 5-120, default 20)."""
    from decimal import Decimal as D

    from meic.adapters.api.server import _watchdog_escalate_seconds

    assert _watchdog_escalate_seconds({}) == D("20")                                        # default
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "30"}) == D("30")   # operator value
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "5"}) == D("5")     # low edge
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "120"}) == D("120") # high edge
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "4"}) == D("20")    # < 5 -> default
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "121"}) == D("20")  # > 120 -> default
    assert _watchdog_escalate_seconds({"MEIC_WATCHDOG_ESCALATE_SECONDS": "junk"}) == D("20")


def test_live_app_wires_a_real_stop_watchdog_task_with_env_thresholds(monkeypatch, tmp_path):
    """STP-03b wiring capstone: `StopWatchdog` (application/watchdog.py) was
    fully written and unit-tested but never constructed anywhere outside its
    own tests (grep confirmed the only references were a health-panel counter
    and an activity-feed icon). This proves live_app() actually constructs a
    REAL StopWatchdog with the env-configured grace/escalate thresholds and
    starts a REAL supervised background task for it — not merely a config
    value nobody reads (the exact 'looks wired, does nothing' class the
    2026-07-10 review finding caught elsewhere in this file)."""
    from decimal import Decimal as D

    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.application.watchdog import StopWatchdog

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_WATCHDOG_GRACE_SECONDS", "12")
    monkeypatch.setenv("MEIC_WATCHDOG_ESCALATE_SECONDS", "25")

    app = live_app()
    comp = app.state.composition

    wd = app.state.stop_watchdog
    assert isinstance(wd, StopWatchdog)
    assert wd.grace_seconds == D("12"), "grace threshold must come from MEIC_WATCHDOG_GRACE_SECONDS"
    assert wd.escalate_seconds == D("25"), "escalate threshold must come from MEIC_WATCHDOG_ESCALATE_SECONDS"
    # the SAME broker/events every other real service is wired against — never
    # a second, disconnected copy of either.
    assert wd.broker is comp.broker
    assert wd.events is comp.events

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect

    with TestClient(app):
        task = getattr(app.state, "stop_watchdog_task", None)
        assert task is not None and not task.done(), (
            "live_app must actually start the STP-03b watchdog loop at startup")


# --- REC-01 / REC-07(8): the live event log must be DURABLE ---------------------

def test_live_app_event_log_is_durable_and_survives_a_rebuild(monkeypatch, tmp_path):
    """Today's gap (v1.54 slice 1): live_app's event log used to be the plain
    in-memory `EventLog`, so a process restart lost the whole log. An event
    appended in one process must be visible after "restarting" — building a
    fresh live_app() over the SAME MEIC_DATA_DIR/state.db — and still show up
    through the ordinary read path (/entries), not just in comp.events."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.application.event_log import DurableEventLog
    from meic.application.market_calendar import trading_day_str
    from meic.domain.events import CondorFilled, DayArmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    # 2026-07-13 fix: /entries is now scoped to TODAY (commands.day(), which
    # here falls back to the real SystemClock's ET TRADING DAY, via the one
    # shared `trading_day_str` helper, since live_app()'s composition wires no
    # fake `day` provider) — so the entry stamped here must fall on that same
    # ET day, or the day-scope filter would (rightly) hide it, and this
    # durability check would fail for a reason unrelated to durability.
    # (DAY-03 live bug, 2026-07-13: `commands.day()` used to derive "today" via
    # `.astimezone()` with no argument, i.e. the OS/operator machine's LOCAL
    # timezone -- not ET, and not UTC either, despite this test's old comment
    # claiming the latter. This test genuinely failed under that bug whenever
    # the runner's local zone disagreed with the ET trading day.)
    today = trading_day_str(datetime.now(timezone.utc))

    app1 = live_app()
    comp1 = app1.state.composition
    assert isinstance(comp1.events, DurableEventLog)

    comp1.events.append(DayArmed(date=today, entry_count=1))
    comp1.events.append(CondorFilled(entry_id=f"{today}#1", net_credit=Decimal("4.00")))

    app2 = live_app()  # a fresh composition over the SAME db file
    comp2 = app2.state.composition
    assert [type(e).__name__ for e in comp2.events] == ["DayArmed", "CondorFilled"]
    assert comp2.events[1].net_credit == Decimal("4.00")

    cards = TestClient(app2).get("/entries").json()
    assert any(c["entry_id"] == f"{today}#1" and c["net_credit"] == "4.00" for c in cards)


def test_day03_entry_survives_the_day_scope_filter_at_23_53_utc_on_a_trading_day(monkeypatch, tmp_path):
    """DAY-03, THE confirmed live bug, reproduced through the REAL production
    wiring (not a unit test of the helper in isolation): `commands.day()`
    (server.py's `build_manual_entry(..., day=...)`) used to derive "today"
    via `datetime.now(timezone.utc).astimezone().date().isoformat()` --
    `.astimezone()` with no argument converts to the SYSTEM's local timezone,
    and (just as importantly) IGNORES any injected clock entirely, always
    reading the REAL wall clock instead.

    Freezing `comp.clock` at 23:53 UTC on 2026-07-13 (19:53 ET, still the
    13th) pins the scenario without depending on the test runner's own OS
    timezone or the real wall-clock date: against the OLD code, `commands.
    day()` ignores this frozen clock and returns whatever day the REAL clock
    reads when the test actually runs (never "2026-07-13" outside one
    coincidental calendar day), so the entry below would NOT be returned by
    /entries' day-scope filter -- exactly the live incident (`2026-07-13#2`
    vanished from the board)."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.application.clocks import MutableClock
    from meic.domain.events import CondorFilled, DayArmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition
    comp.clock = MutableClock(datetime(2026, 7, 13, 23, 53, tzinfo=timezone.utc))

    assert app.state.commands.day() == "2026-07-13"

    comp.events.append(DayArmed(date="2026-07-13", entry_count=1))
    comp.events.append(CondorFilled(entry_id="2026-07-13#2", net_credit=Decimal("4.00")))

    cards = TestClient(app).get("/entries").json()
    assert any(c["entry_id"] == "2026-07-13#2" for c in cards)


def test_day03_mid_session_entry_stamps_todays_et_day_not_a_rolled_over_local_date(monkeypatch, tmp_path):
    """The Tokyo case (operator ruling, DAY-03 fix): 15:00 UTC on 2026-07-13
    is 11:00 EDT -- mid-session -- while a machine in Asia/Tokyo (UTC+9) has
    ALREADY rolled its own local wall-clock date to 2026-07-14. An entry
    fired at this instant must still be stamped "2026-07-13#n", never
    tomorrow's date."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.application.clocks import MutableClock
    from meic.domain.events import CondorFilled, DayArmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition
    comp.clock = MutableClock(datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc))

    assert app.state.commands.day() == "2026-07-13"

    comp.events.append(DayArmed(date="2026-07-13", entry_count=1))
    comp.events.append(CondorFilled(entry_id="2026-07-13#3", net_credit=Decimal("4.00")))

    cards = TestClient(app).get("/entries").json()
    assert any(c["entry_id"] == "2026-07-13#3" for c in cards)


# --- UC-12 v1.56: outage-drill LIVE wiring capstone ----------------------------
# The drill endpoint/command already existed (2628c9d), but with NO live-mode
# semantics: no typed DRILL confirmation gate, a hardcoded 2.0s default instead
# of doc 06's `drill_outage_seconds`, and a honesty note hardcoded to the PAPER
# claim even when run against the real broker. v1.56 requires the confirmation
# gate + a mode-aware honesty note; this pins BOTH on the object live_app()
# actually builds (app.state.commands), not a hand-constructed PanelCommands.

def test_live_app_outage_drill_refuses_without_a_typed_drill_confirmation(monkeypatch, tmp_path):
    """UC-12 v1.56: in LIVE mode (which is what live_app() always is), the
    drill is REFUSED — never run — without confirmation == "DRILL". This must
    short-circuit before ever touching the broker (the unconnected adapter
    would raise), so it is safe to drive through the real HTTP endpoint."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    assert app.state.composition.state.trading_mode == "live"
    client = TestClient(app, headers={"x-api-token": "panel-secret"})

    no_confirmation = client.post("/drill/outage", json={})
    assert no_confirmation.status_code == 400
    assert no_confirmation.json()["detail"] == "confirmation_required"

    wrong_confirmation = client.post("/drill/outage", json={"confirmation": "drill"})  # case-sensitive
    assert wrong_confirmation.status_code == 400


def test_live_app_outage_drill_runs_when_confirmed_with_a_mode_aware_honesty_note(monkeypatch, tmp_path):
    """UC-12 v1.56: a correctly-typed DRILL confirmation runs the drill for
    real, and its evidence carries the LIVE honesty claim (never paper's) —
    swap in a FakeBroker (the real, unconnected TastytradeAdapter cannot
    answer working_orders() offline) to drive it end to end."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from tests.harness.fake_broker import FakeBroker

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    app.state.composition.broker = FakeBroker()   # swap for an offline-safe fake
    client = TestClient(app, headers={"x-api-token": "panel-secret"})

    r = client.post("/drill/outage", json={"confirmation": "DRILL", "outage_seconds": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ok"
    assert "LIVE" in body["honesty_note"]
    assert "PAPER" not in body["honesty_note"]


# --- ENT-08 (v1.9/v1.55) warm-up wiring capstone -------------------------------
# The SIXTH member of the "exists but unwired" class (after RSK-04, the day
# supervisor, TPF/TPT, EC-STP-06's stop-fill detector, ...): `warmup=` has
# existed on LiveRuntime/build_live_runtime since v1.9, but live_app() never
# passed one — the entry never actually warmed anything up. `plan_warmup`
# (application/warmup.py) is a pure decision proven by TC-ENT-06; nothing
# production-side ever called it or ran a real session/chain probe at T-60.

def test_live_app_wires_a_real_warmup_not_none(monkeypatch, tmp_path):
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    runtime = app.state.runtime

    assert runtime.warmup is not None, "ENT-08: live_app() must wire a REAL warm-up"
    assert callable(runtime.warmup)
    assert runtime.warmup_lead_seconds == 60.0   # doc 06 session_warmup_lead_seconds default


def _healthy_side(direction, n: int = 25):
    """A realistic chain side with >= min_validated_strikes(10) reachable,
    two-sided-marked strikes on the default SelectionConfig() — mirrors
    tests/bdd/test_tc_stk_09.py's `_healthy_side` fixture, so the STK-10
    v1.55 baseline this warm-up locks is not a sliver."""
    from meic.domain.chain import ChainSide, Mark

    spot = Decimal("6000")
    strikes = tuple(spot + direction * Decimal(5 * i) for i in range(n))

    def curve(i):
        return max(Decimal("0.15"), Decimal("3.60") - Decimal("0.30") * i)

    marks = {}
    for i, s in enumerate(strikes):
        mid = curve(i)
        marks[s] = Mark(bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05"))
    return ChainSide(strikes, marks)


def test_warmup_actually_primes_session_and_chain_and_locks_the_v155_baseline(monkeypatch, tmp_path):
    """Non-None alone cannot catch a warm-up that is wired but does nothing —
    exactly the 2026-07-10 review-finding pattern pinned by
    test_stop_fill_detector_drives_lex_with_a_real_quote... above. Drive the
    REAL warm-up end-to-end and assert it (1) runs the session probe (a
    clock-drift reading lands), (2) refreshes the held chain snapshot, and
    (3) locks the STK-10 v1.55 baseline for the upcoming entry under the SAME
    (when, entry_number) key the fire will reuse (operator ruling
    2026-07-11) — so the fire's first attempt finds it already locked rather
    than approximating the capture lazily at fire time."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition
    runtime = app.state.runtime
    now = datetime.now(timezone.utc)

    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(now)

    fake_snap = SimpleNamespace(
        spot=Decimal("6000"), stale=False, taken_at=now,
        put_side=_healthy_side(Decimal(-1)), call_side=_healthy_side(Decimal(1)))

    async def _fake_snapshot_chain(session):
        return fake_snap

    import meic.adapters.dxlink.chain_snapshot as _cs_mod
    monkeypatch.setattr(_cs_mod, "snapshot_chain", _fake_snapshot_chain)

    assert runtime.measure_drift_ms() == float("inf")    # nothing probed yet
    assert app.state.chain_snapshots.last is None

    when = datetime(2026, 7, 11, 15, 30, tzinfo=timezone.utc)
    selector = runtime.selector
    assert selector._baseline_key is None

    asyncio.run(runtime.warmup(when, 1, None))

    assert abs(runtime.measure_drift_ms()) < 2000.0      # ENT-08.1/.2: session probe ran
    assert app.state.chain_snapshots.last is fake_snap    # ENT-08.3: chain probe ran

    # v1.55 hook: the SAME (when, entry_number) key the fire will use is
    # already locked -- the fire's first attempt reuses it, never a fresh
    # fire-time capture.
    assert selector._baseline_key == (when, 1)
    assert selector._baseline is not None


def test_drill_guidance_reflects_a_real_near_trigger_short(monkeypatch, tmp_path):
    """UC-12 near-trigger guidance (operator ruling 2026-07-11): the drill
    endpoint's `guidance` must reflect a REAL open short's live trigger-
    distance -- `_drill_guidance_provider` used to hardcode `near_trigger=
    False` unconditionally. Drive it end-to-end: an open PUT short with a
    recorded stop trigger, a chain snapshot mark 80% of the way from fill to
    trigger, and assert the warning actually appears in the drill evidence."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.domain.chain import ChainSide, Mark
    from meic.domain.events import CondorFilled, FilledLeg, StopPlaced
    from tests.harness.fake_broker import FakeBroker

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    app = live_app()
    comp = app.state.composition
    comp.broker = FakeBroker()

    entry_id = "2026-07-11#1"
    put_short_symbol = "SPXW  260711P07535000"      # strike 7535
    comp.events.append(CondorFilled(entry_id=entry_id, net_credit=Decimal("4.00"), legs=(
        FilledLeg(symbol="SPXW  260711P07510000", right="P", role="long", qty=1, price=Decimal("1.00")),
        FilledLeg(symbol=put_short_symbol, right="P", role="short", qty=1, price=Decimal("3.00")),
        FilledLeg(symbol="SPXW  260711C07570000", right="C", role="short", qty=1, price=Decimal("2.00")),
        FilledLeg(symbol="SPXW  260711C07590000", right="C", role="long", qty=1, price=Decimal("1.00")),
    )))
    comp.events.append(StopPlaced(entry_id=entry_id, side="PUT", trigger=Decimal("4.00"),
                                  broker_order_id="STOP-1"))

    # 80% of the way from the 3.00 fill to the 4.00 trigger -- ABOVE the 50%
    # warn threshold (mid of bid 3.75 / ask 3.85 == 3.80).
    put_side = ChainSide(strikes_toward_otm=(Decimal("7535"),),
                        marks={Decimal("7535"): Mark(bid=Decimal("3.75"), ask=Decimal("3.85"))})
    call_side = ChainSide(strikes_toward_otm=(), marks={})
    app.state.chain_snapshots.last = SimpleNamespace(
        stale=False, spot=Decimal("7550"), put_side=put_side, call_side=call_side)

    client = TestClient(app, headers={"x-api-token": "panel-secret"})
    r = client.post("/drill/outage", json={"confirmation": "DRILL", "outage_seconds": 0})
    assert r.status_code == 200
    guidance = r.json()["guidance"]
    assert "a short mark is within 50% of its trigger distance" in guidance


def test_drill_outage_seconds_is_read_from_config_not_hardcoded(monkeypatch, tmp_path):
    """UC-12 `drill_outage_seconds` (doc 06: range 10-300, default 60) must
    come from config — the endpoint used to hardcode 2.0 regardless of the
    dial. Wired onto app.state.commands' default, read at drill time whenever
    a request doesn't specify its own `outage_seconds`."""
    from meic.adapters.api.server import _drill_outage_seconds, live_app

    assert _drill_outage_seconds({}) == 60.0                       # spec default
    assert _drill_outage_seconds({"MEIC_DRILL_OUTAGE_SECONDS": "90"}) == 90.0
    assert _drill_outage_seconds({"MEIC_DRILL_OUTAGE_SECONDS": "9001"}) == 60.0  # out of range
    assert _drill_outage_seconds({"MEIC_DRILL_OUTAGE_SECONDS": "not-a-number"}) == 60.0

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DRILL_OUTAGE_SECONDS", "90")
    app = live_app()
    assert app.state.commands._default_drill_outage_seconds == 90.0


# --- ITEM 1 (operator ruling 2026-07-11): event-driven stop-fill reaction -------
# "the stop being hit triggers the long sale immediately; only if that fails
# does the periodic check force it." TastytradeAdapter.order_events() (the
# account order-status stream, STP-04/ORD-05/LEX-01) already existed and was
# fully contract-tested (test_order_events_account_stream_receives_status) but
# NOTHING in live_app ever consumed it — detect_and_recover_stop_fills ran
# ONLY off the ~60s health tick (_probe_once). This is the SEVENTH member of
# the "exists but unwired" class (RSK-04, the day supervisor, TPF/TPT,
# EC-STP-06's own health-tick wiring, ENT-08 warm-up, ...). Drive the REAL
# live_app(): fake the adapter's order_events() stream to yield ONE
# terminal-filled event and assert a wired consumer reacts by invoking the
# SAME detect_and_recover_stop_fills pass immediately -- not only via the
# ~60s health tick that _connect()'s own boot probe already exercises once.

def test_live_app_wires_a_real_order_event_consumer_that_triggers_the_stop_fill_pass(monkeypatch, tmp_path):
    """ITEM 1 follow-up (operator ruling 2026-07-11): the stop-fill catch-up
    pass no longer rides `_probe_once` at all (it moved to its own dedicated
    poll loop, see `test_stop_fill_poll_loop_drives_the_detector_on_its_own_env_interval`
    below) -- so the boot health probe no longer contributes a call here.
    This test's proof narrows to exactly what it is named for: a
    terminal-filled order event on the REAL wired push consumer triggers the
    pass, with nothing else in this short window able to produce a call
    (the default ~60s health tick and the default 15s fallback poll are both
    far outside the deadline below)."""
    import time as _time

    from fastapi.testclient import TestClient

    import meic.application.stop_fill_watch as sfw
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    calls: list[bool] = []

    async def _fake_pass(comp, alerts, quote_provider):
        calls.append(True)

    monkeypatch.setattr(sfw, "detect_and_recover_stop_fills", _fake_pass)

    app = live_app()
    comp = app.state.composition

    import asyncio

    consumed: list[bool] = []

    async def _fake_order_events():
        consumed.append(True)
        yield {"type": "order_status", "order_id": "1", "status": "filled", "raw": None}
        # Block "forever" (until the consumer task is cancelled on shutdown)
        # rather than exhausting the generator -- an exhausted generator
        # would make the consumer reconnect in a tight loop, re-yielding (and
        # re-reacting to) the same event over and over as fast as the CPU
        # allows, which is not what this test means to exercise.
        await asyncio.Event().wait()

    comp.broker._inner.order_events = _fake_order_events
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect

    with TestClient(app):
        # give the background consumer a beat to read the one fake event and
        # react to it.
        deadline = _time.monotonic() + 3.0
        while not calls and _time.monotonic() < deadline:
            _time.sleep(0.02)

    assert consumed, ("live_app must wire a REAL consumer of the adapter's own "
                      "order_events() stream (STP-04/ORD-05/LEX-01) -- nothing read it")
    assert len(calls) >= 1, (
        "a terminal-filled order event must trigger detect_and_recover_stop_fills "
        "IMMEDIATELY via the push consumer (operator ruling 2026-07-11) -- saw "
        f"{len(calls)} call(s) in the window")


# --- ITEM 1 follow-up (operator ruling 2026-07-11): dedicated fallback poll ----
# The stop-fill FALLBACK poll moved off the ~60s health tick (`_probe_once`,
# which no longer drives it at all) onto its own dedicated loop,
# `MEIC_STOP_FILL_POLL_S` (default 15, range 5-120), skip-if-busy against
# `stop_fill_lock`. These are the wiring capstones for that loop -- the
# eighth "exists but unwired-the-right-way" class fix in this file.

def test_live_app_wires_a_dedicated_stop_fill_poll_loop_with_env_interval(monkeypatch, tmp_path):
    """Non-blocking capstone: the loop's cadence is read from
    MEIC_STOP_FILL_POLL_S (not hardcoded), and the loop task is actually
    created at startup (not merely a config value nobody reads)."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_STOP_FILL_POLL_S", "20")

    app = live_app()
    comp = app.state.composition

    assert app.state.stop_fill_poll_interval_s == 20.0, (
        "the loop's cadence must come from MEIC_STOP_FILL_POLL_S, not a hardcoded value")

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect

    with TestClient(app):
        task = getattr(app.state, "stop_fill_poll_task", None)
        assert task is not None and not task.done(), (
            "live_app must actually start the dedicated stop-fill poll loop at startup")


def test_stop_fill_poll_loop_drives_the_detector_on_its_own_env_interval(monkeypatch, tmp_path):
    """The loop must be a REAL supervised background task that calls the
    SAME wired stop_fill_detector REPEATEDLY on its own env-configured
    cadence -- not merely present-and-inert (the exact 'looks wired, does
    nothing' class the 2026-07-10 review finding caught elsewhere in this
    file). Set MEIC_STOP_FILL_POLL_S to the floor (5s) and require TWO
    calls, deliberately: a lone boot-time call would also happen to pass
    this test under the OLD wiring (`_probe_once`'s own single synchronous
    call at startup, before this loop existed) -- only a genuinely
    REPEATING loop on its own ~5s cadence can produce a second call inside
    this window, since the ~60s health tick and the push consumer (no
    events on this stream) cannot. The order-event stream is left
    permanently open with no events so the push path can't be the source of
    either call."""
    import asyncio
    import time as _time

    from fastapi.testclient import TestClient

    import meic.application.stop_fill_watch as sfw
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_STOP_FILL_POLL_S", "5")

    calls: list[bool] = []

    async def _fake_pass(comp, alerts, quote_provider):
        calls.append(True)

    monkeypatch.setattr(sfw, "detect_and_recover_stop_fills", _fake_pass)

    app = live_app()
    comp = app.state.composition

    async def _never_order_events():
        await asyncio.Event().wait()
        yield {}  # pragma: no cover -- unreachable, keeps this an async generator

    comp.broker._inner.order_events = _never_order_events
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect

    with TestClient(app):
        deadline = _time.monotonic() + 13.0   # >= two 5s ticks, with headroom
        while len(calls) < 2 and _time.monotonic() < deadline:
            _time.sleep(0.05)

    assert len(calls) >= 2, (
        "the dedicated stop-fill poll loop must actually invoke "
        "detect_and_recover_stop_fills REPEATEDLY on MEIC_STOP_FILL_POLL_S's own "
        f"cadence, independent of the push consumer and the ~60s health tick "
        f"(saw {len(calls)} call(s) in the window)")


# --- EOD-03 order-audit sweep wiring capstones (2026-07-11, last two items) ----
# The NINTH member of the "exists but unwired" class: `EndOfDaySweep`
# (application/eod_sweep.py) was complete and unit-tested (incl. the 2026-07-11
# raced-fill flagging) but constructed NOWHERE outside its tests — EOD-03's
# "the day does not end until the bot confirms zero working orders remain"
# never actually ran in live. The wiring mirrors RPT-15's reconcile: a factored
# `_maybe_eod_sweep_once` gate driven by the SAME ~60s health tick, at/after
# the CALENDAR session close (DAY-02 half days, never a hardcoded 16:00), once
# per day, journal-gated on `EodSweepCompleted` so a restart never re-sweeps.

class _EodAlerts:
    def __init__(self):
        self.records = []

    def alert(self, level, message, **context):
        self.records.append((level, message))


class _EodSweepBroker:
    """Minimal broker for the sweep: working orders that disappear when
    cancelled, plus an injectable fills feed for the raced-fill case."""

    def __init__(self, working=(), fills=()):
        self._working = {str(w): {"order_id": str(w)} for w in working}
        self._fills = [dict(f) for f in fills]
        self.cancelled = []

    async def working_orders(self):
        return list(self._working.values())

    async def cancel(self, oid):
        self.cancelled.append(str(oid))
        self._working.pop(str(oid), None)
        return {"result": "cancelled"}

    async def fills_since(self, cursor):
        return list(self._fills)


def _eod_comp(day="2026-07-10", broker=None, events=None):
    from types import SimpleNamespace

    from meic.domain.events import DayArmed

    evs = events if events is not None else []
    evs.insert(0, DayArmed(date=day, entry_count=1))  # RPT-01: a day with activity
    return SimpleNamespace(events=evs, broker=broker or _EodSweepBroker(),
                           alerts=_EodAlerts())


def test_live_app_health_tick_runs_the_eod_03_sweep_gate(monkeypatch, tmp_path):
    """The wiring capstone: `_probe_once` (boot + every health tick) must
    actually invoke the factored EOD-03 sweep gate — same proof shape as the
    push-consumer capstone above (monkeypatch the gate, drive the REAL app
    startup, assert the wired tick called it). Fails at HEAD: the gate does
    not even exist."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from meic.adapters.api import server
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")

    calls = []

    async def _fake_gate(comp, now_fn, **kw):
        calls.append(True)

    monkeypatch.setattr(server, "_maybe_eod_sweep_once", _fake_gate)

    app = live_app()
    comp = app.state.composition
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))

    async def _noop_connect(account=None):   # no network in a unit test
        return None
    comp.connect = _noop_connect

    with TestClient(app):
        pass

    assert calls, ("live_app's health tick (_probe_once) must run the EOD-03 "
                   "sweep gate — EndOfDaySweep is otherwise constructed nowhere")


def test_eod_sweep_waits_for_session_close_then_sweeps_once_per_day(monkeypatch, tmp_path):
    """EOD-03 via the REAL gate: before the session close it does nothing;
    at/after the close it cancels the bot's own working orders, CONFIRMS they
    are gone, and journals `EodSweepCompleted` exactly once (idempotent —
    across ticks and, because the gate reads the journal, across restarts)."""
    import asyncio
    from datetime import datetime

    from meic.adapters.api.server import _maybe_eod_sweep_once
    from meic.domain.events import EodSweepCompleted, StopPlaced

    day = "2026-07-10"
    broker = _EodSweepBroker(working=["S1"])
    comp = _eod_comp(day, broker=broker,
                     events=[StopPlaced(entry_id=f"{day}#1", side="PUT",
                                        trigger=Decimal("4.00"), broker_order_id="S1")])

    from tests.harness.fake_clock import ET as _ET

    before = lambda: datetime(2026, 7, 10, 15, 59, tzinfo=_ET)   # noqa: E731
    after = lambda: datetime(2026, 7, 10, 16, 1, tzinfo=_ET)     # noqa: E731

    asyncio.run(_maybe_eod_sweep_once(comp, before))
    assert broker.cancelled == [] and not any(
        isinstance(e, EodSweepCompleted) for e in comp.events)

    asyncio.run(_maybe_eod_sweep_once(comp, after))
    assert broker.cancelled == ["S1"]
    done = [e for e in comp.events if isinstance(e, EodSweepCompleted)]
    assert len(done) == 1 and done[0].date == day and done[0].cancelled == 1

    asyncio.run(_maybe_eod_sweep_once(comp, after))   # second tick: no re-sweep
    assert broker.cancelled == ["S1"]
    assert sum(isinstance(e, EodSweepCompleted) for e in comp.events) == 1


def test_eod_sweep_uses_the_calendar_session_close_on_half_days(monkeypatch, tmp_path):
    """DAY-02/DAY-01a: a 13:00 half-day close means the sweep runs at 13:01 —
    and does NOT run at 13:01 on an ordinary 16:00 day."""
    import asyncio
    from datetime import date, datetime

    from meic.adapters.api.server import _maybe_eod_sweep_once
    from meic.domain.events import EodSweepCompleted, StopPlaced

    from tests.harness.fake_clock import ET as _ET

    day = "2026-07-10"
    at_1301 = lambda: datetime(2026, 7, 10, 13, 1, tzinfo=_ET)   # noqa: E731

    def _fresh():
        broker = _EodSweepBroker(working=["S1"])
        comp = _eod_comp(day, broker=broker,
                         events=[StopPlaced(entry_id=f"{day}#1", side="PUT",
                                            trigger=Decimal("4.00"), broker_order_id="S1")])
        return broker, comp

    broker, comp = _fresh()
    asyncio.run(_maybe_eod_sweep_once(comp, at_1301))            # full day: too early
    assert broker.cancelled == [] and not any(
        isinstance(e, EodSweepCompleted) for e in comp.events)

    broker, comp = _fresh()
    asyncio.run(_maybe_eod_sweep_once(comp, at_1301,
                                      half_days=frozenset({date(2026, 7, 10)})))
    assert broker.cancelled == ["S1"]
    assert any(isinstance(e, EodSweepCompleted) for e in comp.events)


def test_eod_sweep_flags_raced_fills_critically_and_touches_only_own_orders(monkeypatch, tmp_path):
    """The two teeth the wiring must not lose: (1) OWN-03 — a foreign working
    order (an id the bot never journaled placing) is never cancelled; (2) the
    2026-07-11 raced-fill guard — a bot order that FILLED while being
    cancelled raises EndOfDaySweep's distinct critical alert through
    comp.alerts and is counted `raced_fills`, never reported as a clean
    cancel."""
    import asyncio
    from datetime import datetime

    from meic.adapters.api.server import _maybe_eod_sweep_once
    from meic.domain.events import EodSweepCompleted, StopPlaced

    from tests.harness.fake_clock import ET as _ET

    day = "2026-07-10"
    after = lambda: datetime(2026, 7, 10, 16, 1, tzinfo=_ET)     # noqa: E731
    # S1 is the bot's own (journaled on StopPlaced); F9 is the operator's.
    # S1's cancel races a fill: it leaves working_orders but shows in fills.
    broker = _EodSweepBroker(working=["S1", "F9"],
                             fills=[{"order_id": "S1", "partial": False}])
    comp = _eod_comp(day, broker=broker,
                     events=[StopPlaced(entry_id=f"{day}#1", side="PUT",
                                        trigger=Decimal("4.00"), broker_order_id="S1")])

    asyncio.run(_maybe_eod_sweep_once(comp, after))

    assert broker.cancelled == ["S1"], "OWN-03: the foreign order F9 must never be touched"
    done = [e for e in comp.events if isinstance(e, EodSweepCompleted)]
    assert len(done) == 1 and done[0].raced_fills == 1 and done[0].cancelled == 0
    assert any(level == "critical" and "S1" in msg and "FILLED while being cancelled" in msg
               for level, msg in comp.alerts.records), (
        "the raced-fill critical alert must reach comp.alerts")


def test_eod_sweep_includes_journaled_lex_order_ids(monkeypatch, tmp_path):
    """LEX-01 order-id journaling (v1.62, operator-ratified): LEX orders are
    INCLUDED in the EOD-03 day-end order audit. A `LexOrderPlaced` id (here a
    still-working LEX-05 fallback at the bell) is the bot's OWN order — the
    sweep cancels it; the operator's F9 is still never touched. This was the
    docstring's flagged known-limit, now RESOLVED: before v1.62 the sweep
    would have flagged L7 as a foreign order and left it."""
    import asyncio
    from datetime import datetime

    from meic.adapters.api.server import _journaled_own_order_ids, _maybe_eod_sweep_once
    from meic.domain.events import EodSweepCompleted, LexOrderPlaced

    from tests.harness.fake_clock import ET as _ET

    day = "2026-07-10"
    after = lambda: datetime(2026, 7, 10, 16, 1, tzinfo=_ET)     # noqa: E731
    broker = _EodSweepBroker(working=["L7", "F9"])
    comp = _eod_comp(day, broker=broker,
                     events=[LexOrderPlaced(entry_id=f"{day}#1", side="PUT",
                                            broker_order_id="L7", price=Decimal("0.45"),
                                            kind="fallback")])

    assert "L7" in _journaled_own_order_ids(comp.events), \
        "LexOrderPlaced.broker_order_id must count as the bot's own order"

    asyncio.run(_maybe_eod_sweep_once(comp, after))

    assert broker.cancelled == ["L7"], \
        "the journaled LEX order is swept; the foreign F9 is never touched"
    done = [e for e in comp.events if isinstance(e, EodSweepCompleted)]
    assert len(done) == 1 and done[0].cancelled == 1 and done[0].uncancellable == 0


# --- CLS-03 working-entry cancel wiring capstones (2026-07-11) ------------------
# The TENTH member of the "exists but unwired" class: `ManualClose` (incl. its
# race-guarded `cancel_working`) existed and was unit-tested, but the panel's
# close path only handled FILLED entries via CloseEntry — a WORKING (pre-fill)
# entry had NO close path from the UI at all (PanelCommands.close_as fell into
# `legs_unrecorded`). CLS-03/UC-14/TC-CLS-02: on a WORKING entry the action is
# Cancel entry — cancel the entry order, no close orders for unfilled legs.

class _CancelBroker:
    def __init__(self, fills=()):
        self.cancels = []
        self.submits = 0
        self._fills = [dict(f) for f in fills]

    async def cancel(self, oid):
        self.cancels.append(str(oid))
        return {"result": "cancelled"}

    async def fills_since(self, cursor):
        return list(self._fills)

    async def submit(self, order):
        self.submits += 1
        return "SHOULD-NEVER-HAPPEN"

    async def working_orders(self):
        return []


def _pending_entry_app(monkeypatch, tmp_path, entry_id="2026-07-11#1"):
    from meic.adapters.api.server import live_app
    from meic.domain.events import CondorProposed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()
    comp = app.state.composition
    comp.events.append(CondorProposed(entry_id=entry_id, put_short=Decimal("7500"),
                                      call_short=Decimal("7600")))
    return app, comp


def test_live_app_close_routes_a_working_entry_through_cls_03_cancel(monkeypatch, tmp_path):
    """CLS-03/TC-CLS-02 on the object live_app() actually builds: the panel
    close command on a WORKING entry cancels its entry order via
    ManualClose.cancel_working (CLS-02: the one ratified path — no ad-hoc
    broker calls, no close orders for unfilled legs) and raises the ladder's
    stand-down flag so the reprice ladder cannot race the cancel."""
    import asyncio

    entry_id = "2026-07-11#1"
    app, comp = _pending_entry_app(monkeypatch, tmp_path, entry_id)

    registry = getattr(comp, "working_entries", None)
    assert registry is not None, (
        "CLS-03 needs the composition to carry the working-entry order registry")
    registry.record(entry_id, "ENTRY-ORD-1")
    broker = _CancelBroker()
    comp.broker = broker

    res = asyncio.run(app.state.commands.close(entry_id))

    assert res["result"] == "cancelled" and res["initiator"] == "cancel_entry"
    assert broker.cancels == ["ENTRY-ORD-1"]
    assert broker.submits == 0, "CLS-03: no close orders are placed for unfilled legs"
    assert registry.cancel_requested(entry_id), (
        "the panel must raise the ladder stand-down flag BEFORE cancelling, or the "
        "reprice ladder can replace (live: even resubmit) the order out from under it")


def test_live_app_working_entry_cancel_race_alerts_critically_and_surfaces(monkeypatch, tmp_path):
    """The REPRICE-RACE guard, on the real wiring: the entry FILLS in the
    click→cancel window. The result surfaced to the API is `race_detected`
    (never a clean `cancelled`), a critical alert reaches the panel alert
    sink, and the ReconciliationMismatch lands on the durable log (RSK-03
    blocks new entries until the operator reconciles)."""
    import asyncio

    from meic.domain.events import ReconciliationMismatch

    entry_id = "2026-07-11#1"
    app, comp = _pending_entry_app(monkeypatch, tmp_path, entry_id)
    comp.working_entries.record(entry_id, "ENTRY-ORD-9")
    comp.broker = _CancelBroker(fills=[{"order_id": "ENTRY-ORD-9", "partial": False}])

    res = asyncio.run(app.state.commands.close(entry_id))

    assert res["result"] == "race_detected" and res["initiator"] == "cancel_entry"
    assert any(isinstance(e, ReconciliationMismatch) for e in comp.events)
    assert any(a["level"] == "critical" and "ENTRY-ORD-9" in a["message"]
               for a in app.state.alerts.recent()), (
        "the race must alert critically through the panel sink, not just return")


def test_live_app_close_of_a_pending_entry_with_no_working_order_says_so(monkeypatch, tmp_path):
    """A PENDING entry whose ladder already ended (skipped/never worked) has
    nothing to cancel: the close command reports `no_working_order` — it must
    not fall into the FILLED path's `legs_unrecorded` (that result means a
    fill whose legs the broker never reported, a different fault entirely)."""
    import asyncio

    entry_id = "2026-07-11#1"
    app, comp = _pending_entry_app(monkeypatch, tmp_path, entry_id)
    comp.broker = _CancelBroker()

    res = asyncio.run(app.state.commands.close(entry_id))

    assert res["result"] == "no_working_order"
    assert comp.broker.cancels == [] and comp.broker.submits == 0


# --- PNL-04 "At EOD (and on demand)": POST /reports/reconcile/{day} ------------
# The reconciler (application/report_reconciler.py, wired here as
# `report_reconciler`/`app.state.report_reconciler`) previously ran ONLY from
# the automatic EOD health tick (`_maybe_eod_reconcile_once`) -- there was no
# way for the operator to trigger it on demand, which PNL-04 explicitly
# requires. These capstones drive the REAL live_app() endpoint: it must reuse
# the SAME reconciler/facade the tick uses (never a second, separately-wired
# one), require the SAME auth as every other mutating command, validate the
# day format, and run even on a day the tick's own already-resolved gate
# would skip (a pre-fix legacy CorrectionRecord with no `scope="own"`).

def test_reconcile_endpoint_requires_auth_like_any_other_mutating_command(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()

    client = TestClient(app)  # no x-api-token at all
    r = client.post("/reports/reconcile/2026-07-09")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing_or_bad_token"


def test_reconcile_endpoint_bad_day_format_is_422(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()
    client = TestClient(app, headers={"x-api-token": "panel-secret"})

    r = client.post("/reports/reconcile/not-a-day")
    assert r.status_code == 422


def test_reconcile_endpoint_runs_the_same_reconciler_the_eod_tick_uses(monkeypatch, tmp_path):
    """A valid day, posted with the right token, actually runs
    `report_reconciler.reconcile_day` (the SAME instance `_maybe_eod_reconcile_once`
    calls, per `app.state.report_reconciler`) and returns its outcome as JSON."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.domain.events import DayBrokerConfirmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()
    comp = app.state.composition
    day = "2026-07-09"
    # no activity that day -> an honestly flat/empty day on both sides
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.day_fills = lambda d: _async([])
    comp.broker._inner.day_settlements = lambda d: _async([])

    client = TestClient(app, headers={"x-api-token": "panel-secret"})
    r = client.post(f"/reports/reconcile/{day}")

    assert r.status_code == 200
    body = r.json()
    assert body == {"day": day, "status": "confirmed", "corrections": [],
                    "ambiguous_settlements": 0}
    assert any(isinstance(e, DayBrokerConfirmed) and e.date == day for e in comp.events)


def test_reconcile_endpoint_runs_even_on_a_day_the_eod_gate_would_skip(monkeypatch, tmp_path):
    """The exact case PNL-04's on-demand trigger exists for: a day whose only
    prior record is a pre-fix LEGACY `CorrectionRecord` (no `scope="own"`).
    `_maybe_eod_reconcile_once`'s own gate would skip such a day (see its
    2026-07-12 own-scoping docstring) -- but an explicit operator POST must
    always run regardless."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app
    from meic.domain.events import CorrectionRecord, DayBrokerConfirmed

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()
    comp = app.state.composition
    day = "2026-07-09"
    comp.events.append(CorrectionRecord(date=day, field="fees", bot_value="0",
                                        broker_value="1", diff="1", at="t"))  # legacy: scope=None
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.day_fills = lambda d: _async([])
    comp.broker._inner.day_settlements = lambda d: _async([])

    client = TestClient(app, headers={"x-api-token": "panel-secret"})
    r = client.post(f"/reports/reconcile/{day}")

    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert any(isinstance(e, DayBrokerConfirmed) and e.date == day for e in comp.events)


def test_reconcile_endpoint_surfaces_an_unreachable_broker_rather_than_swallowing_it(monkeypatch, tmp_path):
    """The reconciler's own "unreachable" outcome (any broker read raising) must
    reach the operator through this endpoint, never be caught-and-hidden."""
    from fastapi.testclient import TestClient

    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    app = live_app()
    comp = app.state.composition

    async def _boom():
        raise ConnectionError("down")

    comp.broker._inner.positions = _boom

    client = TestClient(app, headers={"x-api-token": "panel-secret"})
    r = client.post("/reports/reconcile/2026-07-09")

    assert r.status_code == 200
    assert r.json()["status"] == "unreachable"
