"""reporting.periods — RPT-01 pure period-bucket filtering."""
from decimal import Decimal as D

from meic.domain.events import CondorFilled, DayArmed, EntrySkipped, ModeSwitchStaged
from meic.domain.projection import fold
from meic.reporting.periods import resolve_period, scope_events


def test_resolve_period_day_month_year_all():
    days = ("2026-06-30", "2026-07-08", "2026-07-09", "2027-01-02")
    assert resolve_period(days, day="2026-07-09") == ("2026-07-09",)
    assert resolve_period(days, month="2026-07") == ("2026-07-08", "2026-07-09")
    assert resolve_period(days, year="2026") == ("2026-06-30", "2026-07-08", "2026-07-09")
    assert resolve_period(days, period="today", today="2026-07-09") == ("2026-07-09",)
    assert resolve_period(days) == days  # "all" / unspecified


def test_scope_events_filters_entry_and_day_scoped_events():
    events = [
        DayArmed(date="2026-07-08", entry_count=1),
        CondorFilled(entry_id="2026-07-08#1", net_credit=D("4.00")),
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.00")),
        EntrySkipped(date="2026-07-09", entry_number=2, reason="not_armed"),
        ModeSwitchStaged(target="live", effective="next_day"),  # unscoped -> passes through
    ]
    scoped = scope_events(events, ("2026-07-09",))
    assert set(fold(scoped).entries) == {"2026-07-09#1"}
    assert any(isinstance(e, EntrySkipped) for e in scoped)
    assert not any(isinstance(e, CondorFilled) and e.entry_id == "2026-07-08#1" for e in scoped)
    assert any(isinstance(e, ModeSwitchStaged) for e in scoped)  # passthrough, not day-scoped


def test_scope_events_empty_scope_drops_everything_scoped():
    events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    assert scope_events(events, ()) == []
