"""CAL-03/CAL-04 layered removal semantics (final-review finding 1, 2026-07-15).

The bug this pins fail-first: `NoTradeTagRemoved` used to BOTH pop the manual
tag AND add the day to `removed_days` (the CAL-04 auto-tag suppression set) in
one shot. Poisoning sequence: the operator manually tags a day, untags it,
and only LATER sets "always block FOMC" + imports an FOMC schedule that
includes that day -- the stale `removed_days` entry made `effective_tags`
skip it forever, so the gate TRADED on an FOMC day the operator believed was
covered, with no visible indication anywhere.

Fixed semantics (LAYERED, an implementation decision -- doc 11 is silent on
the manual/auto collision; flagged in trading_calendar.py for operator
reversal, same C-flag culture as doc 11's own decisions):

  * removal pops the MANUAL layer first, never touching `removed_days`;
  * only a removal on a day with NO manual tag but an EFFECTIVE auto-tag
    suppresses the auto layer (persisting across rule re-add/re-import,
    unchanged CAL-04 behaviour);
  * a removal on a day with neither layer is a harmless idempotent no-op.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.calendar_store import CalendarStore
from meic.application.entry_gates import GateSnapshot
from meic.application.event_log import EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.events import EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from meic.domain.trading_calendar import effective_tags
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
FOMC_DAY = "2026-09-16"
NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)   # 10:00 ET on the FOMC day


def _store():
    events = EventLog(config_version="v1.71")
    clock = FakeClock(NOW)
    return events, clock, CalendarStore(events, clock)


def _condor(n=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 9, 16), contracts=1)


def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def test_cal04_stale_manual_removal_never_poisons_a_later_auto_tag():
    """THE poisoning sequence, driven end-to-end through the real gate: the
    entry must be BLOCKED `blackout:FOMC` (before the fix it traded)."""
    events, clock, store = _store()

    # 1. manual tag, then manual untag -- long before any rule exists
    store.tag(FOMC_DAY, "note to self")
    store.untag(FOMC_DAY)
    # 2. LATER: standing rule + an import that includes that very day
    store.set_standing_rule("FOMC")
    store.import_events(category="FOMC", dates=[FOMC_DAY, "2026-10-28"])

    # the tag store view: the day IS covered
    assert store.label_for_day(FOMC_DAY) == "FOMC"
    assert effective_tags(store.state())[FOMC_DAY].origin == "auto"

    # and the scheduled-entry gate actually BLOCKS (CAL-05, entries only)
    broker = FakeBroker()
    broker.autofill(lambda o: o.kind == "iron_condor")
    execute = ExecuteEntryAttempt(broker, clock, events, SPX)
    state = PersistentState(InMemoryStateStore())
    state.armed = True
    state.confirm_live = True
    runner = RunTradingDay(clock, state, execute, events,
                           market_gates=_gates(), calendar_label=store.label_for_day)
    filled = asyncio.run(runner.run(FOMC_DAY, [ScheduledEntry(when=NOW, condor=_condor(1))]))

    assert filled == 0
    skips = [e for e in events if isinstance(e, EntrySkipped)]
    assert [s.reason for s in skips] == ["blackout:FOMC"]


def test_cal03_cal04_dual_layer_removal_is_layered_manual_first_then_auto():
    """Manual tag + auto tag on the SAME day: the first removal pops only the
    manual layer (the day stays visibly auto-tagged); the second suppresses
    the auto layer; a third is a harmless no-op."""
    _, _, store = _store()
    store.set_standing_rule("FOMC")
    store.import_events(category="FOMC", dates=[FOMC_DAY])
    store.tag(FOMC_DAY, "my own label")                      # manual layer on top

    assert effective_tags(store.state())[FOMC_DAY].origin == "manual"

    store.untag(FOMC_DAY)                                    # 1st: manual layer only
    tag = effective_tags(store.state()).get(FOMC_DAY)
    assert tag is not None and tag.origin == "auto" and tag.label == "FOMC"
    assert FOMC_DAY not in store.state().removed_days

    store.untag(FOMC_DAY)                                    # 2nd: suppress the auto layer
    assert FOMC_DAY not in effective_tags(store.state())
    assert FOMC_DAY in store.state().removed_days

    before = store.state()
    store.untag(FOMC_DAY)                                    # 3rd: neither layer -- no-op
    assert store.state() == before


def test_cal04_auto_suppression_still_survives_rule_readd_and_reimport():
    """The unchanged CAL-04 half: a genuinely-suppressed auto-tag day stays
    suppressed across rule removal/re-add AND a fresh import naming it."""
    _, _, store = _store()
    store.set_standing_rule("FOMC")
    store.import_events(category="FOMC", dates=[FOMC_DAY, "2026-10-28"])
    store.untag(FOMC_DAY)                                    # no manual layer -> suppresses

    store.remove_standing_rule("FOMC")
    store.set_standing_rule("FOMC")
    store.import_events(category="FOMC", dates=[FOMC_DAY, "2026-12-16"])

    tags = effective_tags(store.state())
    assert FOMC_DAY not in tags                              # never resurrected
    assert "2026-12-16" in tags                              # the rule still auto-tags


def test_cal03_removal_on_an_untagged_day_is_an_idempotent_noop():
    """A removal with neither layer present must change nothing -- in
    particular it must NOT plant a `removed_days` landmine for the future
    (that is exactly the poisoning sequence above)."""
    _, _, store = _store()
    store.untag(FOMC_DAY)
    assert FOMC_DAY not in store.state().removed_days
    assert store.state().manual_tags == {}
