"""TC-CAL-01 -- CAL-01->05/07 tags, tiers, enforcement (doc 11, v1.71).

Binds the BACKEND halves of every scenario: the tag/rule fold
(domain/trading_calendar.py), the operator mutation surface
(application/calendar_store.py), and CAL-05's ENT-06 gate wiring
(application/execute_entry.py / application/run_trading_day.py). The
frontend-only clauses ("visually distinct") are bound against the backend
DATA that drives that rendering (the `origin` field) -- slice 2 owns the
actual pixels; nothing here fakes a UI assertion.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import EventJournal
from meic.application.calendar_store import CalendarStore
from meic.application.entry_gates import GateSnapshot
from meic.application.event_log import DurableEventLog, EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.events import EntrySkipped
from meic.domain.projection import day_report, fold
from meic.domain.ticks import TickRung, TickTable
from meic.domain.trading_calendar import effective_tags
from meic.domain.trading_calendar import fold as calendar_fold
from meic.domain.trading_calendar import tier_for_category
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import FakeClock, FastClock

scenarios("../features/TC-CAL-01.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)   # a FOMC Wednesday, 10:00 ET
DAY = "2026-07-15"

# CAL-05's own text: "stops, LEX, TPF, TPT, decay, EOD, and reconcile run
# untouched" -- structural pin. None of these application-layer modules may
# ever import the calendar tag store or call `evaluate_filters`; if one did,
# a blackout would silently start touching management, which CAL-05/C1
# explicitly forbids.
_MANAGEMENT_MODULES = (
    "watchdog.py", "lex_ladder_watchdog.py", "recover_long.py", "tpf_monitor.py",
    "tpt_monitor.py", "decay_watcher.py", "eod_sweep.py", "reconcile.py",
    "reconcile_boot.py", "close_entry.py", "protect_position.py", "stop_fill_watch.py",
)
_APPLICATION_DIR = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic" / "application"


def _condor(n=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 15), contracts=1)


def _all_pass_gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _fresh_store(start: datetime = NOW):
    events = EventLog(config_version="v1.71")
    clock = FakeClock(start)
    return events, clock, CalendarStore(events, clock)


@pytest.fixture
def world():
    return {}


# --- Scenario 1: a tagged day blocks scheduled entries and nothing else ------

@given('today is tagged NO-TRADE with label "FOMC"')
def _(world):
    events, clock, store = _fresh_store()
    store.tag(DAY, "FOMC")
    world.update(events=events, clock=clock, store=store)


@then('every scheduled entry skips with reason "blackout:FOMC" shown on its card')
def _(world):
    events, clock, store = world["events"], world["clock"], world["store"]
    broker = FakeBroker()
    execute = ExecuteEntryAttempt(broker, clock, events, SPX)
    from meic.adapters.persistence.event_store import InMemoryStateStore
    state = PersistentState(InMemoryStateStore())
    state.armed = True
    state.confirm_live = True

    runner = RunTradingDay(clock, state, execute, events,
                           market_gates=_all_pass_gates(),
                           calendar_label=store.label_for_day)   # CAL-05
    schedule = [ScheduledEntry(when=NOW, condor=_condor(1)),
                ScheduledEntry(when=NOW, condor=_condor(2))]
    filled = asyncio.run(runner.run(DAY, schedule))

    assert filled == 0
    skips = [e for e in events if isinstance(e, EntrySkipped)]
    assert len(skips) == 2
    assert {s.reason for s in skips} == {"blackout:FOMC"}
    # the day report's own skip list -- what the card renders (get_report()'s
    # `skips` field / get_entries() for a filled entry).
    report = day_report(events, DAY)
    assert report.skips == ((1, "blackout:FOMC"), (2, "blackout:FOMC"))
    world["events"] = events


@then('stops, LEX, TPF, TPT, decay, EOD, and reconcile run untouched')
def _(world):
    """Structural absence pin (CAL-05/C1): no management module ever imports
    the calendar tag store or calls the ENT-06 filter evaluator -- a
    blackout is Stop-Trading-for-one-scheduled-day, never a position action."""
    for name in _MANAGEMENT_MODULES:
        text = (_APPLICATION_DIR / name).read_text(encoding="utf-8")
        assert "calendar_store" not in text and "CalendarStore" not in text, (
            f"{name} must never consult the calendar tag store (CAL-05)")
        assert "evaluate_filters" not in text, (
            f"{name} must never call the ENT-06 filter evaluator (CAL-05)")


# --- Scenario 2: a standing category rule auto-tags imported events ---------

@given('a standing rule "always block FOMC" and a fresh FOMC schedule import')
def _(world):
    events, clock, store = _fresh_store()
    store.set_standing_rule("FOMC")
    store.import_events(category="FOMC", dates=["2026-07-15", "2026-09-16", "2026-10-28"])
    world.update(events=events, clock=clock, store=store)


@then('every imported FOMC day is auto-tagged, visually distinct, and individually removable')
def _(world):
    store = world["store"]
    tags = effective_tags(store.state())
    for d in ("2026-07-15", "2026-09-16", "2026-10-28"):
        assert tags[d].label == "FOMC"
        # "visually distinct" (UI-30, slice 2) renders off THIS field --
        # origin="auto" vs "manual" is the backend data the frontend keys its
        # styling on; the pixels themselves are out of scope for this slice.
        assert tags[d].origin == "auto"
        assert tags[d].category == "FOMC"

    store.untag("2026-07-15")   # individually removable
    world["removed_day"] = "2026-07-15"


@then('removing one day leaves the rule and other days intact')
def _(world):
    store = world["store"]
    tags = effective_tags(store.state())
    assert world["removed_day"] not in tags
    assert "2026-09-16" in tags and "2026-10-28" in tags
    assert "FOMC" in store.state().standing_rules   # the rule itself survives

    # a LATER import for the same category still auto-tags (the rule was
    # never removed) -- and the removed day stays removed even though this
    # fresh import names it again (CAL-04: "removing ... does not resurrect
    # individually-removed days").
    store.import_events(category="FOMC", dates=["2026-07-15", "2026-12-16"])
    tags = effective_tags(store.state())
    assert "2026-12-16" in tags and tags["2026-12-16"].origin == "auto"
    assert world["removed_day"] not in tags


# --- Scenario 3: empty calendar means trade; staleness shown never blocking -

@given("no imports and no tags")
def _(world):
    events, clock, store = _fresh_store()
    world.update(events=events, clock=clock, store=store)


@then("no entry is blocked by the calendar")
def _(world):
    store = world["store"]
    assert store.label_for_day(DAY) is None

    events, clock = world["events"], world["clock"]
    broker = FakeBroker()
    broker.autofill(lambda o: o.kind == "iron_condor")
    execute = ExecuteEntryAttempt(broker, clock, events, SPX)
    from meic.adapters.persistence.event_store import InMemoryStateStore
    state = PersistentState(InMemoryStateStore())
    state.armed = True
    state.confirm_live = True
    runner = RunTradingDay(clock, state, execute, events,
                           market_gates=_all_pass_gates(), calendar_label=store.label_for_day)
    filled = asyncio.run(runner.run(DAY, [ScheduledEntry(when=NOW, condor=_condor(1))]))
    assert filled == 1   # untagged calendar blocked nothing (CAL-07)


@then("an import older than cal_stale_after_days banners the calendar as stale without blocking")
def _(world):
    events, clock, store = world["events"], world["clock"], world["store"]
    store.import_events(category="CPI", dates=["2026-07-11"])
    clock.set_time(clock.now() + timedelta(days=46))   # older than the default 45

    report = store.staleness_report(stale_after_days=45)
    assert report["CPI"].stale is True

    # still never blocks: the imported CPI date carries no TAG (staleness is
    # display-only, CAL-02/CAL-07), so an entry on ANY day -- including the
    # stale import's own date -- is untouched by staleness alone.
    assert store.label_for_day("2026-07-11") is None


# --- Scenario 4: tags and rules survive a reboot -----------------------------

@given("tags and a standing rule exist")
def _(world, tmp_path):
    journal = EventJournal(tmp_path / "cal-reboot.db")
    events = DurableEventLog(config_version="v1.71", journal=journal)
    clock = FastClock(NOW)
    store = CalendarStore(events, clock)
    store.tag("2026-07-20", "CPI print")
    store.set_standing_rule("FOMC", label="FOMC day")
    store.import_events(category="FOMC", dates=["2026-07-29"])

    world["pre_reboot_state"] = store.state()
    world["journal_path"] = tmp_path / "cal-reboot.db"
    journal.close()


@when("the bot restarts")
def _(world):
    # "Crash/restart in the doc-04 harness is exactly: close this object,
    # open a new one on the SAME file, replay" (event_store.py's own
    # docstring) -- REC-07's v1.71 extension needs no bespoke restore path.
    reopened = EventJournal(world["journal_path"])
    world["restored_state"] = calendar_fold(reopened.load())
    reopened.close()


@then("both restore exactly per REC-07")
def _(world):
    assert world["restored_state"] == world["pre_reboot_state"]
    tags = effective_tags(world["restored_state"])
    assert tags["2026-07-20"].label == "CPI print" and tags["2026-07-20"].origin == "manual"
    assert tags["2026-07-29"].label == "FOMC day" and tags["2026-07-29"].origin == "auto"


# --- Scenario 5: tier-2 events are never trusted silently --------------------

@given("imported Fed-speaker events")
def _(world):
    events, clock, store = _fresh_store()
    store.import_events(category="FED_SPEAKER", dates=["2026-07-16"], source="pasted_table")
    world.update(events=events, clock=clock, store=store)


@then("they render visually distinct as tier-2 and days without data show no fabricated events")
def _(world):
    store = world["store"]
    # tier-2, distinctly from a tier-1 category (UI-30 keys its "tier-2,
    # display-only in trust terms" styling off THIS split).
    assert tier_for_category("FED_SPEAKER") == 2
    assert tier_for_category("FOMC") == 1

    imp = store.state().imports["FED_SPEAKER"]
    assert imp.dates == frozenset({"2026-07-16"})

    # a day with no import shows NOTHING -- never a fabricated event/tag.
    assert store.label_for_day("2026-07-17") is None
    assert "2026-07-17" not in effective_tags(store.state())
