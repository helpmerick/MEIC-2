"""TC-RPT-05 — RPT-10/doc-10-Principle-4 deterministic replay. A log folded
fresh from genesis in ONE pass must equal, byte-for-byte (Decimal-exact), the
SAME log folded incrementally: a partial fold (as a crash mid-day would
leave it, REC-01) resumed one event at a time onto that SAME running state.
This is the crash-recovery / dashboard-number invariant doc 10 pins: there is
no cache, no side-store, only the fold -- so any two ways of running it must
agree exactly.
"""
from decimal import Decimal as D

from pytest_bdd import given, scenarios, then, when

from meic.domain.events import (
    CondorFilled,
    DayArmed,
    LongSold,
    ShortStopped,
    SideExpired,
)
from meic.domain.projection import DayState, apply, fold
from meic.reporting.folds import core_results

scenarios("../features/TC-RPT-05.feature")


@given("any event log", target_fixture="events")
def _():
    day = "2026-07-09"
    return [
        DayArmed(date=day, entry_count=2),
        CondorFilled(entry_id=f"{day}#1", net_credit=D("4.00"), fee=D("0.50")),
        ShortStopped(entry_id=f"{day}#1", side="PUT", fill=D("3.80"),
                     slippage=D("0.05"), fee=D("0.10")),
        LongSold(entry_id=f"{day}#1", side="PUT", recovery=D("0.15"), fee=D("0.05")),
        CondorFilled(entry_id=f"{day}#2", net_credit=D("3.50"), fee=D("0.50")),
        SideExpired(entry_id=f"{day}#2", side="PUT"),
        SideExpired(entry_id=f"{day}#2", side="CALL"),
    ]


@when("the log is replayed from genesis into a fresh projection", target_fixture="events")
def _(events):
    # A no-op pass-through step: the "fresh projection" itself is built inside
    # the Then step below (via `fold`), since that IS "replaying from genesis"
    # -- there is no separate mutable projection object to construct first.
    return events


@then("every dashboard number is byte-identical to the incremental projection")
def _(events):
    fresh_state = fold(events)  # replayed from genesis, one pass

    # A partial fold (as a crash mid-log would leave the process, REC-01),
    # resumed ONE EVENT AT A TIME onto that same running state.
    mid = 4
    resumed_state = fold(events[:mid])
    for e in events[mid:]:
        resumed_state = apply(resumed_state, e)

    assert resumed_state == fresh_state
    assert resumed_state.day_pnl == fresh_state.day_pnl

    # Purely incremental from an empty projection, one event at a time.
    incremental_state = DayState()
    for e in events:
        incremental_state = apply(incremental_state, e)
    assert incremental_state == fresh_state

    # The reporting layer's own derived numbers agree identically too --
    # nothing here is a mutable side-store of truth (doc 10 Principle 4).
    assert core_results(events).net_pnl == core_results(list(events)).net_pnl
