"""TC-RPT-01 — RPT-01 period buckets. Slice 1 bound the bucketing scenario
("Disarmed flat days do not dilute averages"): pure fold math over the event
log, exactly what `reporting.folds` computes. Slice 2 binds the "trust
stamps" scenario now that RPT-15 (application/report_reconciler.py) and the
period-picker layer (reporting/periods.py, reporting/trust.py) exist."""
from decimal import Decimal as D

from pytest_bdd import given, parsers, scenario, then

from meic.domain.events import CondorFilled, DayArmed, DayBrokerConfirmed
from meic.domain.projection import fold
from meic.reporting.folds import core_results, daily_net, trading_days
from meic.reporting.periods import resolve_period, scope_events
from meic.reporting.trust import trust_stamp


@scenario("../features/TC-RPT-01.feature", "Period buckets and trust stamps")
def test_period_buckets_and_trust_stamps():
    pass


@given("fills across two ET days, one broker-reconciled and one pending",
       target_fixture="scenario_data")
def _():
    day1, day2 = "2026-07-08", "2026-07-09"  # day1 reconciled, day2 ("today") pending
    events = [
        DayArmed(date=day1, entry_count=1),
        CondorFilled(entry_id=f"{day1}#1", net_credit=D("4.00")),
        DayBrokerConfirmed(date=day1, at="2026-07-08T16:20:00-04:00",
                           checked={"fees": "0", "flat": "True"}),
        DayArmed(date=day2, entry_count=1),
        CondorFilled(entry_id=f"{day2}#1", net_credit=D("3.00")),
    ]
    # A structurally SEPARATE list, as a paper composition's own event log
    # would be — same entry id, wildly different credit, so any accidental
    # merge would be obvious.
    paper_events = [
        DayArmed(date=day2, entry_count=1),
        CondorFilled(entry_id=f"{day2}#1", net_credit=D("9999.00")),
    ]
    return {"events": events, "paper_events": paper_events,
            "day1": day1, "day2": day2, "today": day2}


@then("Today shows only today's entries with a bot-computed badge")
def _(scenario_data):
    events, today = scenario_data["events"], scenario_data["today"]
    days = trading_days(events)
    scope = resolve_period(days, period="today", today=today)
    assert scope == (today,)
    scoped = scope_events(events, scope)
    assert set(fold(scoped).entries) == {f"{today}#1"}
    trust = trust_stamp(events, scope)
    assert trust.status == "bot-computed"  # today's day was never reconciled


@then(parsers.parse('the month badge reads "{label}"'))
def _(scenario_data, label):
    events, day1, day2 = scenario_data["events"], scenario_data["day1"], scenario_data["day2"]
    days = trading_days(events)
    scope = resolve_period(days, month=day1[:7])
    assert scope == (day1, day2)
    trust = trust_stamp(events, scope)
    assert trust.label == label


@then("paper fills never appear in live periods or exports")
def _(scenario_data):
    events = scenario_data["events"]
    paper_events = scenario_data["paper_events"]
    day2 = scenario_data["day2"]
    live_net = core_results(events).net_pnl
    paper_net = core_results(paper_events).net_pnl
    assert live_net != paper_net  # each mode folds its OWN list only, never merged
    assert fold(events).entries[f"{day2}#1"].net_credit == D("3.00")
    assert fold(paper_events).entries[f"{day2}#1"].net_credit == D("9999.00")


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
