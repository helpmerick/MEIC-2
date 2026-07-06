"""Deterministic replay projection — REC-01 substance (see also TC-REC-01)."""
from decimal import Decimal as D

from meic.domain.events import (
    CondorFilled,
    CondorProposed,
    DayArmed,
    DayCompleted,
    EntryCompleted,
    LongSold,
    ShortStopped,
    SideExpired,
)
from meic.domain.projection import DayState, fold


def canonical_day():
    """A scripted day: 2 entries, one whipsaw (both sides stop), one clean."""
    return [
        DayArmed(date="2026-07-06", entry_count=2),
        CondorProposed(entry_id="e1", put_short=D("5990"), call_short=D("6060")),
        CondorFilled(entry_id="e1", net_credit=D("4.00")),
        CondorProposed(entry_id="e2", put_short=D("5985"), call_short=D("6065")),
        CondorFilled(entry_id="e2", net_credit=D("2.30")),
        # e1: put side stops at 3.80, long recovers 0; call side expires -> +0.20
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0.15")),
        LongSold(entry_id="e1", side="PUT", recovery=D("0.00")),
        SideExpired(entry_id="e1", side="CALL"),
        EntryCompleted(entry_id="e1"),
        # e2: both sides expire worthless -> keep full 2.30
        SideExpired(entry_id="e2", side="PUT"),
        SideExpired(entry_id="e2", side="CALL"),
        EntryCompleted(entry_id="e2"),
        DayCompleted(date="2026-07-06"),
    ]


def test_outcome_contract_pnl():
    """Ash's contract: one-side hit nets the kept 5%; e1 = 4.00 - 3.80 = +0.20."""
    state = fold(canonical_day())
    assert state.entries["e1"].pnl == D("0.20")
    assert state.entries["e2"].pnl == D("2.30")
    assert state.day_pnl == D("2.50")


def test_both_sides_stopped_loses_about_the_premium():
    events = [
        DayArmed(date="d", entry_count=1),
        CondorFilled(entry_id="e", net_credit=D("4.00")),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
        ShortStopped(entry_id="e", side="CALL", fill=D("3.80"), slippage=D("0")),
    ]
    # 4.00 - 3.80 - 3.80 = -3.60 (about the premium, never more before slippage)
    assert fold(events).entries["e"].pnl == D("-3.60")


def test_replay_is_deterministic():
    """REC-01: replaying the same log reproduces identical state + P&L."""
    events = canonical_day()
    assert fold(events) == fold(events)
    assert fold(events).day_pnl == fold(events).day_pnl


def test_replay_from_persisted_roundtrip_is_identical(tmp_path):
    """The log persisted and reloaded folds to an equal DayState."""
    from meic.adapters.persistence.event_store import SqliteEventStore

    original = fold(canonical_day())
    store = SqliteEventStore(tmp_path / "log.db")
    store.append("day-2026-07-06", canonical_day())
    store.close()

    reloaded = fold(SqliteEventStore(tmp_path / "log.db").read("day-2026-07-06"))
    assert reloaded == original
    assert reloaded.day_pnl == original.day_pnl == D("2.50")


def test_incremental_fold_equals_full_fold():
    """Folding prefix-by-prefix (crash mid-day, resume) lands where a single
    full fold does."""
    events = canonical_day()
    incremental = DayState()
    from meic.domain.projection import apply
    for e in events:
        incremental = apply(incremental, e)
    assert incremental == fold(events)
