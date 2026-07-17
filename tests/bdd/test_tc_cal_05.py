"""TC-CAL-05 -- CAL-11 event proximity warnings (doc 11, v1.84).

Binds all four scenarios against the BACKEND halves: the pure
domain/trading_calendar.py `active_warnings` fold, application/
calendar_store.py's `active_warnings`/`dismiss_warning` operator-facing
surface, and a structural absence pin proving the warning feed is never
wired into the ENT-03/ENT-06 gate chain (CAL-11 rule 1: "purely
informational"). The frontend-only clauses (the banner's own rendering,
dismiss button) are a separate (not yet built) UI slice; this file is
backend-only, honestly, same discipline test_tc_cal_03.py/test_tc_cal_04.py
already use for their own doc-11 slices.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then

from meic.adapters.persistence.event_store import EventJournal, InMemoryStateStore
from meic.application.calendar_store import CalendarStore
from meic.application.entry_gates import GateSnapshot
from meic.application.event_log import DurableEventLog, EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.market_calendar import is_trading_day
from meic.application.nyse_holidays import holidays_near
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.trading_calendar import fold as calendar_fold
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import FakeClock

scenarios("../features/TC-CAL-05.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))

# 2026-07-15 is a FOMC Wednesday (same real-calendar vector TC-CAL-01 uses).
# 2026-07-11/12 (Sat/Sun) sit between T-3 (Fri 2026-07-10) and the event --
# the real weekend the "trading days, not calendar days" clause proves.
FOMC_DAY = "2026-07-15"
T3 = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)   # Friday
T2 = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)   # Monday
T1 = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)   # Tuesday
T0 = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)   # Wednesday, the event itself

# CAL-11 rule 1 ("purely informational ... changes NO gate, blocks NO entry")
# -- structural absence pin, same convention as TC-CAL-01's own
# `_MANAGEMENT_MODULES` pin: the warning feed must never be importable from
# anything gate/entry-related.
_GATE_MODULES = ("entry_gates.py", "run_trading_day.py")
_APPLICATION_DIR = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic" / "application"


def _fresh_store(start: datetime):
    events = EventLog(config_version="v1.84")
    clock = FakeClock(start)
    return events, clock, CalendarStore(events, clock)


def _all_pass_gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _condor(n=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 17), contracts=1)


@pytest.fixture
def world():
    return {}


# --- Scenario 1: Warnings appear day-of/T-1/2/3, never blocking -------------

@given("an FOMC event and event_warning_lead_days = 3")
def _(world):
    events, clock, store = _fresh_store(T3)
    store.import_events(category="FOMC", dates=[FOMC_DAY])
    world.update(events=events, clock=clock, store=store, lead_days=3)


@then("a dismissable banner shows on the day and 1, 2, and 3 trading days before")
def _(world):
    store, clock, lead_days = world["store"], world["clock"], world["lead_days"]
    for when, expected_tier in ((T3, 3), (T2, 2), (T1, 1), (T0, 0)):
        clock.set_time(when)
        warnings = store.active_warnings(lead_days=lead_days)
        matches = [w for w in warnings if w.category == "FOMC" and w.event_date == FOMC_DAY]
        assert len(matches) == 1, f"expected exactly one FOMC warning at {when}"
        assert matches[0].proximity_tier == expected_tier


@then("no entry is ever blocked or gated by the warning")
def _(world):
    """Structural absence pin (CAL-11 rule 1): neither the ENT-06 filter
    snapshot machinery nor the day supervisor's own scheduler may ever
    reference the warning feed or its dismissal event -- a banner is
    display-only and must be structurally incapable of gating an entry."""
    for name in _GATE_MODULES:
        text = (_APPLICATION_DIR / name).read_text(encoding="utf-8")
        assert "active_warnings" not in text, (
            f"{name} must never consult the CAL-11 warning feed")
        assert "EventWarningDismissed" not in text, (
            f"{name} must never reference the CAL-11 dismissal event")


@then("the countdown is measured in trading days so a weekend is skipped")
def _(world):
    # 2026-07-11/12 (Sat/Sun) sit between T-3 (Fri 2026-07-10) and the FOMC
    # day (Wed 2026-07-15) -- 5 CALENDAR days apart, yet exactly 3 TRADING
    # days (the previous Then step's T3 -> proximity_tier == 3 assertion
    # already depended on this): the weekend is skipped, never counted.
    holidays = holidays_near(date(2026, 7, 10))
    assert not is_trading_day(date(2026, 7, 11), holidays=holidays)
    assert not is_trading_day(date(2026, 7, 12), holidays=holidays)


# --- Scenario 2: Dismissal is per-event-per-tier, never pre-silences -------

@given("the operator dismisses the T-3 FOMC banner")
def _(world, tmp_path):
    journal_path = tmp_path / "cal-warn-reboot.db"
    journal = EventJournal(journal_path)
    events = DurableEventLog(config_version="v1.84", journal=journal)
    clock = FakeClock(T3)
    store = CalendarStore(events, clock)
    store.import_events(category="FOMC", dates=[FOMC_DAY])
    store.dismiss_warning("FOMC", FOMC_DAY, 3)
    world.update(events=events, clock=clock, store=store,
                 journal=journal, journal_path=journal_path)


@then("the T-2, T-1, and day-of FOMC banners still appear as the event approaches")
def _(world):
    store, clock = world["store"], world["clock"]
    # T-3 is dismissed -- no warning at that tier, for THIS event.
    warnings = store.active_warnings(lead_days=3)
    assert not any(w.category == "FOMC" and w.proximity_tier == 3 for w in warnings)
    # T-2/T-1/T-0 are untouched by the T-3 dismissal -- each tier independent.
    for when, expected_tier in ((T2, 2), (T1, 1), (T0, 0)):
        clock.set_time(when)
        warnings = store.active_warnings(lead_days=3)
        matches = [w for w in warnings if w.category == "FOMC" and w.event_date == FOMC_DAY]
        assert len(matches) == 1 and matches[0].proximity_tier == expected_tier


@then("re-dismissing a given tier never re-nags, across restarts (REC-07)")
def _(world):
    world["journal"].close()
    reopened = EventJournal(world["journal_path"])
    restored_events = reopened.load()
    reopened.close()

    restored_state = calendar_fold(restored_events)
    assert ("FOMC", FOMC_DAY, 3) in restored_state.dismissed_warnings

    # A FRESH store over the restored events/journal-less list: T-3 stays
    # dismissed post-restart (never re-nagged)...
    replay_list = list(restored_events)
    store_at_t3 = CalendarStore(replay_list, FakeClock(T3))
    warnings_t3 = store_at_t3.active_warnings(lead_days=3)
    assert not any(w.category == "FOMC" and w.proximity_tier == 3 for w in warnings_t3)

    # ...while T-2/T-1/T-0 remain fully unaffected by that same dismissal.
    for when, expected_tier in ((T2, 2), (T1, 1), (T0, 0)):
        store_at = CalendarStore(replay_list, FakeClock(when))
        warnings_at = store_at.active_warnings(lead_days=3)
        matches = [w for w in warnings_at if w.category == "FOMC" and w.event_date == FOMC_DAY]
        assert len(matches) == 1 and matches[0].proximity_tier == expected_tier


# --- Scenario 3: Warnings are honest and never fabricated ------------------

@given("a tier-2 Fed-speaker event")
def _(world):
    events, clock, store = _fresh_store(T0)
    store.import_events(category="FED_SPEAKER", dates=[FOMC_DAY], source="pasted_table")
    world.update(events=events, clock=clock, store=store)


@then("its banner is labeled best-effort, never stated as certain")
def _(world):
    store = world["store"]
    warnings = store.active_warnings(lead_days=3)
    speaker = [w for w in warnings if w.category == "FED_SPEAKER"]
    assert len(speaker) == 1
    assert speaker[0].tier == 2
    assert speaker[0].best_effort is True   # CAL-01: tier-2 is never certain
    assert speaker[0].computed is False


@then("no banner ever appears for an event not on the calendar")
def _(world):
    store = world["store"]
    warnings = store.active_warnings(lead_days=3)
    # never fabricated: among IMPORTED (tier-1/tier-2) categories, only the
    # actually-imported FED_SPEAKER date appears -- no warning for a
    # category/date this store never carried. (Computed OPEX_MONTHLY/
    # QUAD_WITCH warnings also legitimately appear in this window -- CAL-11
    # explicitly lists them among "every category" that gets a warning; that
    # is not fabrication, since they are genuinely computed for this date
    # range, just not what THIS assertion is scoped to.)
    imported_only = {(w.category, w.event_date) for w in warnings if not w.computed}
    assert imported_only == {("FED_SPEAKER", FOMC_DAY)}
    assert not any(w.category == "CPI" for w in warnings)
    assert not any(w.event_date == "2026-07-16" for w in warnings)


# --- Scenario 4: A warning is not a tag -------------------------------------

@given("an untagged OpEx day with its warning showing")
def _(world):
    opex_day = "2026-07-17"   # third Friday of July 2026 (OPEX_MONTHLY, TC-CAL-04)
    events, clock, store = _fresh_store(
        datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc))
    world.update(events=events, clock=clock, store=store, opex_day=opex_day)


@then("entries still fire normally (the warning informs, CAL-05 enforces only on tags)")
def _(world):
    store, events, clock, opex_day = (
        world["store"], world["events"], world["clock"], world["opex_day"])

    # untagged AND its day-of warning shows -- both true simultaneously.
    assert store.label_for_day(opex_day) is None
    warnings = store.active_warnings(lead_days=0)
    assert any(w.category == "OPEX_MONTHLY" and w.event_date == opex_day
               and w.proximity_tier == 0 for w in warnings)

    # a scheduled entry on this exact day still fills -- the warning changed
    # nothing about the ENT-06 gate (CAL-05 enforces only on tags).
    broker = FakeBroker()
    broker.autofill(lambda o: o.kind == "iron_condor")
    execute = ExecuteEntryAttempt(broker, clock, events, SPX)
    state = PersistentState(InMemoryStateStore())
    state.armed = True
    state.confirm_live = True
    runner = RunTradingDay(clock, state, execute, events,
                           market_gates=_all_pass_gates(), calendar_label=store.label_for_day)
    filled = asyncio.run(runner.run(opex_day, [ScheduledEntry(when=clock.now(), condor=_condor(1))]))
    assert filled == 1
