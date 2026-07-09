"""THE live-wiring capstone (v1.47, operator-approved).

Items 4-7 built RSK-04, RSK-08, the ENT-03 buying-power gate, the ENT-04 per-entry
rows and the ENT-09 fire — and every one of them was armed in the PAPER
composition and in tests, while `live_app()` constructed a LiveRuntime with all
three rails left at None and threw the schedule rows away. The live day would have
traded 1 contract per row at the global premium/width/stop, with no max-day-risk
ceiling and no order cap.

That happened because the tests built a LiveRuntime themselves. So these tests
assert on the REAL wiring functions — `build_live_runtime`, `build_manual_entry`,
`schedule_rows`, `live_preflight_checks` — the same ones live_app calls. Adding a
rail to LiveRuntime and forgetting to wire it must fail HERE.
"""
import asyncio
from dataclasses import fields
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.application.schedule_service import ScheduleService
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.composition.live_wiring import (
    BrokerClockProbe,
    CountingBroker,
    build_live_runtime,
    build_manual_entry,
    live_preflight_checks,
    max_day_risk_of,
    open_worst_cases,
    schedule_rows,
)
from meic.domain.events import CondorFilled, EntryClosed, ReconciliationMismatch
from meic.domain.risk import OrderCap
from meic.domain.stop_policy import StopBasis

ET = timezone.utc
TODAY = date(2026, 7, 8)

# Every LiveRuntime field that is a SAFETY RAIL. If one is None in live wiring,
# a rule the spec mandates is simply not running.
SAFETY_RAILS = ("max_day_risk", "order_cap", "buying_power")


class _Broker:
    def __init__(self, bp=D("100000")):
        self._bp = bp
        self.submits = 0
        self.replaces = 0

    async def buying_power(self):
        return self._bp

    async def submit(self, order):
        self.submits += 1
        return f"O-{self.submits}"

    async def replace(self, oid, new):
        self.replaces += 1
        return f"R-{self.replaces}"

    async def working_orders(self):
        return []


class _Comp:
    def __init__(self, state=None, broker=None):
        self.state = state or _state()
        self.broker = broker or _Broker()
        self.events: list = []
        self.worst_case: dict = {}
        self.clock = None
        self.execute = None

    async def _on_filled(self, entry_id, condor, stop=None):
        pass


def _state(rows=None, max_day_risk="20000"):
    st = PersistentState(InMemoryStateStore())
    out = ScheduleService(st).save(rows if rows is not None else [{"time": "10:00"}],
                                   max_day_risk=max_day_risk)
    # a silently-rejected save would leave an EMPTY schedule and every assertion
    # below would pass vacuously
    assert out["result"] == "saved", out
    return st


async def _selector(when, n, config=None):
    return None, "data_unavailable"


async def _gates():
    raise AssertionError("gates should not be called by wiring construction")


def _runtime(comp, **kw):
    return build_live_runtime(comp, selector=_selector, market_gates=_gates, **kw)


# --- THE capstone --------------------------------------------------------------

def test_no_safety_rail_is_none_in_the_real_live_wiring():
    """The one that would have caught the defect. Every rail armed, by the very
    function live_app() calls."""
    runtime = _runtime(_Comp())

    for rail in SAFETY_RAILS:
        assert getattr(runtime, rail) is not None, f"{rail} is not wired in live"

    assert runtime.max_day_risk == D("20000")           # RSK-04, from the panel
    assert isinstance(runtime.order_cap, OrderCap)      # RSK-08
    assert asyncio.run(runtime.buying_power()) == D("100000")   # ENT-03, the real BP


def test_every_liveruntime_rail_is_accounted_for():
    """A tripwire. Add a rail to LiveRuntime and this test fails until you either
    wire it in build_live_runtime or state, here, that it is not a rail."""
    NOT_RAILS = {"comp", "selector", "market_gates", "warmup", "max_entries_per_day",
                 "warmup_lead_seconds", "max_clock_drift_ms", "measure_drift_ms"}
    known = set(SAFETY_RAILS) | NOT_RAILS
    actual = {f.name for f in fields(LiveRuntime)}
    assert actual <= known, f"unclassified LiveRuntime field(s): {actual - known}"


def test_a_missing_ceiling_is_none_not_a_silent_infinity():
    """doc 06 §169 makes max_day_risk mandatory before live; UC-02's pre-flight is
    what refuses the arm. The rail reports 'unconfigured', never 'unlimited'."""
    comp = _Comp(state=_state(max_day_risk=None))
    assert _runtime(comp).max_day_risk is None
    assert max_day_risk_of(comp.state) is None


# --- ENT-04: the rows reach the live day ---------------------------------------

def test_schedule_rows_carry_each_rows_own_settings_not_just_its_time():
    """The live day used to keep only the times. Every row then traded 1 contract
    at the global premium/width/stop, whatever the panel displayed."""
    state = _state([{"time": "10:00", "contracts": 2, "wing_width": "30"},
                    {"time": "11:15", "contracts": 1, "stop_loss_pct": 150}])

    rows = schedule_rows(state, today=TODAY, tz=ET)

    assert [r.when.hour for r in rows] == [10, 11]
    assert [r.entry.contracts for r in rows] == [2, 1]
    assert rows[0].selection.contracts == 2 and rows[0].selection.wing_width == D("30")
    assert rows[1].stop.pct == D("150")
    assert rows[0].stop.basis is StopBasis.TOTAL_CREDIT


def test_schedule_rows_are_sorted_and_dated_today():
    """Save already enforces strictly-increasing times; the sort is belt-and-braces
    for a schedule that reached durable state some other way."""
    state = _state([{"time": "10:00"}, {"time": "11:15"}])
    state.entry_schedule = list(reversed(state.entry_schedule))   # bypass Save's ordering

    rows = schedule_rows(state, today=TODAY, tz=ET)

    assert [r.when.time() for r in rows] == [time(10, 0), time(11, 15)]
    assert all(r.when.date() == TODAY for r in rows)


def test_every_scheduled_row_carries_a_resolved_entry():
    """A ScheduledRow with entry=None means 'use the globals' — never in live."""
    rows = schedule_rows(_state([{"time": "10:00"}, {"time": "11:15"}]), today=TODAY, tz=ET)
    assert all(isinstance(r, ScheduledRow) and r.entry is not None for r in rows)


# --- RSK-08: the cap is actually counted ---------------------------------------

def test_the_order_cap_counts_submits_and_replaces_at_the_broker():
    """A cap nobody increments is not a cap. RSK-08: a replace IS a new order."""
    comp = _Comp()
    runtime = _runtime(comp, daily_order_cap=50, order_cap_buffer=5)

    assert isinstance(comp.broker, CountingBroker)
    asyncio.run(comp.broker.submit({}))
    asyncio.run(comp.broker.replace("O-1", {}))
    assert runtime.order_cap.count == 2


def test_the_counting_broker_passes_everything_else_through():
    comp = _Comp()
    _runtime(comp)
    assert asyncio.run(comp.broker.buying_power()) == D("100000")
    assert asyncio.run(comp.broker.working_orders()) == []


def test_wrapping_is_idempotent_so_a_restart_does_not_double_count():
    comp = _Comp()
    first = _runtime(comp).order_cap
    inner = comp.broker
    second = _runtime(comp).order_cap
    assert comp.broker is inner              # not wrapped twice
    assert first is not second               # a new day gets a fresh count


def test_the_cap_blocks_new_entries_but_never_exit_side_orders():
    cap = _runtime(_Comp(), daily_order_cap=2, order_cap_buffer=1).order_cap
    cap.record()                              # 1 of (2 - 1)
    assert cap.allow(exit_priority=False) is False   # entries blocked at the buffer
    assert cap.allow(exit_priority=True) is True     # stops/LEX/flatten never blocked


# --- RSK-04 shares ONE book with the manual fire --------------------------------

def test_the_runtime_and_the_composition_share_one_worst_case_book():
    """A manual ENT-09 fire and the scheduled day must not each keep their own
    idea of the day's exposure."""
    comp = _Comp()
    runtime = _runtime(comp)
    assert runtime._worst_case is comp.worst_case

    comp.worst_case["d#1"] = D("4600")
    comp.events.append(CondorFilled(entry_id="d#1", net_credit=D("4.00")))
    assert open_worst_cases(comp) == (D("4600"),)


def test_a_closed_entry_leaves_the_exposure_total():
    comp = _Comp()
    comp.worst_case["d#1"] = D("4600")
    comp.events.append(CondorFilled(entry_id="d#1", net_credit=D("4.00")))
    comp.events.append(EntryClosed(entry_id="d#1", initiator="take_profit"))
    assert open_worst_cases(comp) == ()


def test_the_manual_fire_reads_the_same_ceiling_and_the_same_open_exposure():
    """ENT-09: the manual entry crosses the IDENTICAL rails."""
    comp = _Comp()
    _runtime(comp)                                     # installs the CountingBroker
    comp.worst_case["d#1"] = D("6000")
    comp.events.append(CondorFilled(entry_id="d#1", net_credit=D("4.00")))

    manual = build_manual_entry(comp, selector=_selector, market_gates=_gates)
    risk = asyncio.run(manual._risk())

    assert risk.max_day_risk == D("20000")             # the panel's ceiling
    assert risk.open_worst_cases == (D("6000"),)       # the same open book
    assert risk.buying_power == D("100000")            # the same real BP
    assert risk.order_cap_allows_entry is True
    assert risk.new_worst_case == D("0")               # attempt() re-prices it


# --- UC-02: the pre-flight checks are real --------------------------------------

def _probe(drift_ms=0.0):
    """A BrokerClockProbe holding one fresh reading `drift_ms` behind the broker.
    server time = local - drift, so record() computes exactly `drift_ms`."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    pr = BrokerClockProbe(now=lambda: now)
    pr.record(now - timedelta(milliseconds=drift_ms), local_time=now)
    return pr


def _checks(comp, *, fresh=True, drift_ms=0.0):
    return live_preflight_checks(comp, data_fresh=lambda: fresh, drift=_probe(drift_ms))


def test_the_preflight_checks_are_real_not_trivially_passing():
    comp = _Comp()
    checks = _checks(comp)
    assert {"reconcile", "clock", "config", "market_data"} == set(checks)
    assert all(fn()[0] for fn in checks.values())


def test_an_unresolved_reconciliation_mismatch_blocks_the_arm():
    comp = _Comp()
    comp.events.append(ReconciliationMismatch(detail="position mismatch"))
    ok, detail = _checks(comp)["reconcile"]()
    assert ok is False and "REC-02" in detail


def test_clock_drift_beyond_tolerance_blocks_the_arm():
    ok, detail = _checks(_Comp(), drift_ms=3000.0)["clock"]()   # > 2000ms default
    assert ok is False and "RSK-07" in detail
    assert _checks(_Comp(), drift_ms=-500.0)["clock"]()[0] is True   # inside tolerance


def test_stale_chain_data_blocks_the_arm():
    ok, detail = _checks(_Comp(), fresh=False)["market_data"]()
    assert ok is False and "DAT-02" in detail


def test_an_unsaved_schedule_has_no_config_version_and_blocks_the_arm():
    comp = _Comp(state=PersistentState(InMemoryStateStore()))   # never saved
    ok, detail = _checks(comp)["config"]()
    assert ok is False and "save the schedule" in detail


# --- DAY-03: an unverified clock blocks, it is never assumed to be zero ---------

def test_an_unmeasured_clock_is_unverified_and_blocks_the_arm():
    """DAY-03: the clock MUST be verified against an authoritative source. Nothing
    measures drift here, so wiring `lambda: 0.0` would assert a perfect clock
    forever — a rail that can never fire, which is worse than no rail because the
    pre-flight would tick green."""
    drift = BrokerClockProbe()                 # no reading recorded
    assert drift.verified is False
    assert drift.ms() == float("inf")          # blocks, never passes

    checks = live_preflight_checks(_Comp(), data_fresh=lambda: True, drift=drift)
    ok, detail = checks["clock"]()
    assert ok is False and "DAY-03" in detail


def test_a_stale_reading_is_unverified_and_blocks_the_arm():
    """v1.48: the latest reading older than 300 s counts as unmeasured -- the probe
    may have silently stopped landing."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    clock = {"now": t0}
    drift = BrokerClockProbe(now=lambda: clock["now"])
    drift.record(t0, local_time=t0)             # 0 ms drift, fresh
    assert drift.verified is True and drift.ms() == 0.0

    clock["now"] = t0 + timedelta(seconds=301)  # the probe went quiet
    assert drift.verified is False and drift.ms() == float("inf")


def test_an_unreadable_date_header_clears_the_reading():
    """server_time() returns None when the broker sends no Date header (or the
    probe fails). That must not leave a stale value looking fresh."""
    from datetime import datetime, timezone
    t0 = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    drift = BrokerClockProbe(now=lambda: t0)
    drift.record(t0, local_time=t0)
    assert drift.verified is True
    drift.record(None)                          # no header this probe
    assert drift.verified is False and drift.ms() == float("inf")


def test_an_unverified_clock_blocks_every_entry_in_the_runtime():
    from meic.application.entry_gates import clock_drift_blocks_entry

    runtime = _runtime(_Comp())                # no probe reading yet
    assert clock_drift_blocks_entry(drift_ms=runtime.measure_drift_ms(),
                                    max_drift_ms=runtime.max_clock_drift_ms) is True


def test_a_probed_drift_inside_tolerance_passes():
    probe = _probe(500.0)                       # 500ms, under the 2000ms default
    runtime = _runtime(_Comp(), drift=probe)
    assert runtime.measure_drift_ms() == 500.0
    assert live_preflight_checks(_Comp(), data_fresh=lambda: True,
                                 drift=probe)["clock"]()[0] is True


# --- ENT-09: the manual fire honours the reconcile block and clock drift --------

def test_the_manual_fire_is_blocked_by_an_unresolved_reconcile_mismatch():
    """ENT-09 names these explicitly; they sit OUTSIDE the ENT-03 gate chain, so
    attempt() cannot run them and ManualEntry must."""
    comp = _Comp()
    _runtime(comp)
    comp.events.append(ReconciliationMismatch(detail="position mismatch"))
    manual = build_manual_entry(comp, selector=_selector, market_gates=_gates,
                                drift=_probe(0.0))
    assert manual._blocks() == "reconcile_pending"


def test_the_manual_fire_is_blocked_by_clock_drift():
    comp = _Comp()
    _runtime(comp)
    assert build_manual_entry(comp, selector=_selector, market_gates=_gates,
                              drift=_probe(3000.0))._blocks() == "clock_drift"
    # ... and by an UNVERIFIED clock, which reads as infinite drift
    assert build_manual_entry(comp, selector=_selector, market_gates=_gates,
                              drift=BrokerClockProbe())._blocks() == "clock_drift"


def test_a_clear_manual_fire_has_no_block():
    comp = _Comp()
    _runtime(comp)
    assert build_manual_entry(comp, selector=_selector, market_gates=_gates,
                              drift=_probe(10.0))._blocks() is None


def test_preflight_predicates_are_synchronous():
    """They run in a sync FastAPI handler on a threadpool; awaiting the broker
    from there would bind its session to a fresh event loop."""
    import inspect

    for fn in _checks(_Comp()).values():
        assert not inspect.iscoroutinefunction(fn)
        assert not inspect.isawaitable(fn())


def test_manual_fire_awaits_an_async_risk_provider():
    """Regression (live 500): live's risk provider is ASYNC — it awaits a real
    buying-power call. The manual fire path must AWAIT it via _maybe_await, not pass
    an un-awaited coroutine into execute.attempt (where dataclasses.replace() blew
    up with 'replace() should be called on dataclass instances')."""
    import asyncio
    import datetime
    from types import SimpleNamespace

    from meic.application.entry_gates import RiskSnapshot
    from meic.application.manual_entry import ManualEntry

    captured = {}

    class _Execute:
        async def attempt(self, **kw):
            captured["risk"] = kw["risk"]
            return SimpleNamespace(status="SKIPPED", reason="stop_here")

    st = _state()
    st.armed = True
    st.confirm_live = True
    st.stop_trading = False                       # -> entries_enabled()
    comp = _Comp(state=st)
    comp.execute = _Execute()
    comp.clock = SimpleNamespace(now=lambda: datetime.datetime(2026, 7, 9, 12, 0))

    row = ScheduleService(st).resolved()[0]

    async def async_risk():                        # exactly like build_manual_entry
        return RiskSnapshot(new_worst_case=D("0"), buying_power=D("3448"))

    async def sel(when, n, config=None):
        return SimpleNamespace(entry_number=1), None

    async def gates():
        return object()

    manual = ManualEntry(comp, sel, gates, risk=async_risk, day=lambda: "2026-07-09")
    asyncio.run(manual.fire(press_id="p1", entry_number=1, row=row, confirmed=True))

    assert isinstance(captured["risk"], RiskSnapshot)   # awaited, not a coroutine
    assert captured["risk"].buying_power == D("3448")
