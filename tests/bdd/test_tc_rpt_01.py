"""TC-RPT-01 — RPT-01 period buckets. Slice 1 binds ONLY the bucketing
scenario ("Disarmed flat days do not dilute averages"): pure fold math over
the event log, exactly what `reporting.folds` computes. The "trust stamps"
scenario needs RPT-15 broker-reconcile + a period-picker/export layer that
does not exist yet (deferred — see the slice-1 handoff notes)."""
from decimal import Decimal as D

from pytest_bdd import given, scenario, then

from meic.domain.events import CondorFilled, DayArmed
from meic.reporting.folds import daily_net, trading_days


@scenario("../features/TC-RPT-01.feature", "Disarmed flat days do not dilute averages")
def test_disarmed_flat_days_do_not_dilute_averages():
    pass


@given("5 trading days and 2 disarmed flat days in a month", target_fixture="events")
def _():
    events = []
    # 5 qualifying trading days: each armed and firing exactly one entry.
    for i, net in enumerate([D("400"), D("20"), D("-360"), D("400"), D("20")]):
        day = f"2026-07-{10 + i:02d}"
        events.append(DayArmed(date=day, entry_count=1))
        events.append(CondorFilled(entry_id=f"{day}#1", net_credit=net / 100))
    # 2 DISARMED flat days emit NOTHING at all: server.py's `_supervise_once`
    # never starts the day task while disarmed, so no DayArmed/EntrySkipped/
    # entry ever lands in the log for them -- they are excluded from the
    # trading-day set BY CONSTRUCTION, never by a post-hoc filter.
    return events


@then("day-based means and win rates use n=5")
def _(events):
    days = trading_days(events)
    assert len(days) == 5
    daily = daily_net(events)
    assert len(daily) == 5
    assert sorted(daily.values()) == sorted([D("400"), D("20"), D("-360"), D("400"), D("20")])
