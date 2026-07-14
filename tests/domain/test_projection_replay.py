"""Deterministic replay projection — REC-01 substance (see also TC-REC-01)."""
from decimal import Decimal as D

from meic.domain.events import (
    CondorFilled,
    CondorProposed,
    DayArmed,
    DayCompleted,
    EntryClosed,
    EntrySkipped,
    FilledLeg,
    LongSold,
    ShortStopped,
    SideExpired,
)
from meic.domain.projection import DayState, day_report, fold


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
        # e2: both sides expire worthless -> keep full 2.30
        SideExpired(entry_id="e2", side="PUT"),
        SideExpired(entry_id="e2", side="CALL"),
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


def test_fees_reduce_entry_pnl_pnl01():
    """PNL-01/02: recorded per-fill fees subtract from entry P&L. The seam is
    designed now; the FeeModel that POPULATES the fee lands in slice 2/3."""
    events = [
        DayArmed(date="d", entry_count=1),
        CondorFilled(entry_id="e", net_credit=D("4.00"), fee=D("0.08")),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0"), fee=D("0.02")),
        LongSold(entry_id="e", side="PUT", recovery=D("0.00"), fee=D("0.02")),
        SideExpired(entry_id="e", side="CALL"),
    ]
    # 4.00 - 3.80 + 0 - (0.08 + 0.02 + 0.02) = 0.08
    assert fold(events).entries["e"].pnl == D("0.08")


def test_fee_field_defaults_and_old_log_without_fee_still_replays():
    """Schema evolution: an event dict written before `fee` existed replays
    with fee defaulting to 0 (no KeyError)."""
    from meic.domain.events import Event
    old = {"type": "CondorFilled", "entry_id": "e", "net_credit": "4.00"}  # no fee key
    ev = Event.from_dict(old)
    assert ev.fee == D("0")
    assert fold([ev]).entries["e"].pnl == D("4.00")


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


def test_condor_filled_at_and_legs_carry_through_to_the_projection():
    """FEATURE 1/2 (card): CondorFilled's `at` and `legs` project straight onto
    the EntryProjection so the API layer can build the card's placed_at/legs/
    premium_received without re-reading the event log itself."""
    legs = (
        FilledLeg(symbol="SPXW260709P07535000", right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol="SPXW260709P07510000", right="P", role="long", qty=1, price=D("0.08")),
        FilledLeg(symbol="SPXW260709C07540000", right="C", role="short", qty=1, price=D("1.95")),
        FilledLeg(symbol="SPXW260709C07565000", right="C", role="long", qty=1, price=D("0.07")),
    )
    events = [
        DayArmed(date="d", entry_count=1),
        CondorFilled(entry_id="e", net_credit=D("3.60"), legs=legs, at="2026-07-09T14:32:00+00:00"),
    ]
    e = fold(events).entries["e"]
    assert e.placed_at == "2026-07-09T14:32:00+00:00"
    assert e.legs == legs


def test_condor_filled_without_at_or_legs_projects_null_placed_at():
    """Schema evolution / paper fills: no `at`, no legs -> an honest empty card."""
    events = [DayArmed(date="d", entry_count=1), CondorFilled(entry_id="e", net_credit=D("4.00"))]
    e = fold(events).entries["e"]
    assert e.placed_at is None
    assert e.legs == ()


def test_incremental_fold_equals_full_fold():
    """Folding prefix-by-prefix (crash mid-day, resume) lands where a single
    full fold does."""
    events = canonical_day()
    incremental = DayState()
    from meic.domain.projection import apply
    for e in events:
        incremental = apply(incremental, e)
    assert incremental == fold(events)


# --- EOD-01 v1.59: SettlementRecorded folds into the entry ------------------

def _condor_legs():
    return (
        FilledLeg(symbol="P1", right="P", role="short", qty=1, price=D("2.20")),
        FilledLeg(symbol="P2", right="P", role="long", qty=1, price=D("0.40")),
        FilledLeg(symbol="C1", right="C", role="short", qty=1, price=D("2.15")),
        FilledLeg(symbol="C2", right="C", role="long", qty=1, price=D("0.35")),
    )


def test_settlement_recorded_accumulates_value_and_fee_by_entry():
    from meic.domain.events import SettlementRecorded

    events = [
        CondorFilled(entry_id="e", net_credit=D("3.60"), legs=_condor_legs()),
        SettlementRecorded(entry_id="e", day="d", at="t1", symbol="C1", sub_type="Cash Settled Assignment",
                           quantity=1, price=D("7540"), value=D("-369.00"), fee=D("5.00")),
        SettlementRecorded(entry_id="e", day="d", at="t2", symbol="P1", sub_type="Expiration",
                           quantity=1, price=None, value=D("0"), fee=D("0")),
    ]
    e = fold(events).entries["e"]
    assert e.settlements == D("-369.00")
    assert e.settlement_fees == D("5.00")
    assert e.settled_symbols == frozenset({"C1", "P1"})
    # SettlementRecorded never touches the per-share `.pnl` -- only entry_dollars does.
    assert e.pnl == D("3.60")


def test_settlement_pending_true_until_every_unstopped_short_is_captured():
    from meic.domain.events import SettlementRecorded

    events = [CondorFilled(entry_id="e", net_credit=D("3.60"), legs=_condor_legs())]
    assert fold(events).entries["e"].settlement_pending is True  # nothing captured yet

    events.append(SettlementRecorded(entry_id="e", day="d", at="t1", symbol="C1",
                                     sub_type="Cash Settled Assignment", quantity=1,
                                     price=D("7540"), value=D("-369.00"), fee=D("5.00")))
    assert fold(events).entries["e"].settlement_pending is True  # P1 (the other short) still pending

    events.append(SettlementRecorded(entry_id="e", day="d", at="t2", symbol="P1",
                                     sub_type="Expiration", quantity=1, price=None,
                                     value=D("0"), fee=D("0")))
    assert fold(events).entries["e"].settlement_pending is False  # both shorts captured now


def test_settlement_pending_false_for_a_stopped_side_regardless_of_settlement():
    """A side that already stopped realized its P&L via the fill -- it has
    nothing left pending, with or without a settlement record."""
    events = [
        CondorFilled(entry_id="e", net_credit=D("3.60"), legs=_condor_legs()),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
    ]
    assert fold(events).entries["e"].settlement_pending is True  # CALL short (C1) still pending

    from meic.domain.events import SettlementRecorded
    events.append(SettlementRecorded(entry_id="e", day="d", at="t1", symbol="C1",
                                     sub_type="Cash Settled Assignment", quantity=1,
                                     price=D("7540"), value=D("-369.00"), fee=D("5.00")))
    assert fold(events).entries["e"].settlement_pending is False


def test_settlement_pending_false_once_the_entry_is_closed_some_other_way():
    events = [
        CondorFilled(entry_id="e", net_credit=D("3.60"), legs=_condor_legs()),
        EntryClosed(entry_id="e", initiator="eod"),
    ]
    assert fold(events).entries["e"].settlement_pending is False


def test_settlement_pending_false_with_no_recorded_legs():
    events = [CondorFilled(entry_id="e", net_credit=D("4.00"))]  # no legs (paper/schema gap)
    assert fold(events).entries["e"].settlement_pending is False


# --- 2026-07-13 fix: day_report must be DAY-SCOPED --------------------------
#
# day_report used to fold the ENTIRE log with no day filter at all -- so a
# prior day's entry that never reached a terminal state (its settlement never
# captured) counted toward every later day's totals forever. Pinned to the
# real vector observed live: 2026-07-10#1 (credit 5.20, fee 4.80 -> pnl 0.40,
# still lingering unsettled) + 2026-07-13#2 (credit 2.80, fee 2.30 -> pnl
# 0.50, today's actual fill).

def _two_day_log():
    return [
        DayArmed(date="2026-07-10", entry_count=1),
        CondorFilled(entry_id="2026-07-10#1", net_credit=D("5.20"), fee=D("4.80")),
        DayArmed(date="2026-07-13", entry_count=2),
        CondorFilled(entry_id="2026-07-13#2", net_credit=D("2.80"), fee=D("2.30")),
    ]


def test_day_report_explicit_day_scopes_to_only_that_days_entries():
    events = _two_day_log()

    today = day_report(events, "2026-07-13")
    assert today.entries_filled == 1
    assert today.total_credit == D("2.80")
    assert today.day_pnl == D("0.50")
    assert today.per_entry_pnl == {"2026-07-13#2": D("0.50")}

    prior = day_report(events, "2026-07-10")
    assert prior.entries_filled == 1
    assert prior.total_credit == D("5.20")
    assert prior.day_pnl == D("0.40")
    assert prior.per_entry_pnl == {"2026-07-10#1": D("0.40")}


def test_day_report_with_no_day_arg_defaults_to_state_date_not_the_whole_log():
    """A caller that doesn't pass `day` (get_report in app.py) still gets
    "today" -- state.date, the most recent DayArmed -- never the whole log."""
    events = _two_day_log()
    default = day_report(events)
    assert default.date == "2026-07-13"
    assert default.entries_filled == 1
    assert default.total_credit == D("2.80")
    assert default.day_pnl == D("0.50")


def test_day_report_skips_scope_to_the_requested_day_too():
    events = [
        DayArmed(date="2026-07-10", entry_count=1),
        EntrySkipped(date="2026-07-10", entry_number=1, reason="max_entries"),
        DayArmed(date="2026-07-13", entry_count=1),
        EntrySkipped(date="2026-07-13", entry_number=1, reason="stale_clock"),
    ]
    assert day_report(events, "2026-07-13").skips == ((1, "stale_clock"),)
    assert day_report(events, "2026-07-10").skips == ((1, "max_entries"),)


def test_day_report_with_no_date_anywhere_stays_unscoped():
    """No DayArmed at all in this log (e.g. TC-CLS-02's synthetic "e1" fixture,
    which never arms a day) -- there is no day concept to scope by, so every
    entry is still reported, matching the only behaviour such a log ever had."""
    events = [EntryClosed(entry_id="e1", initiator="manual")]
    rpt = day_report(events)
    assert rpt.date is None
