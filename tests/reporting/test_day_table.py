"""reporting.day_table -- RPT-17/UI-33 pure helpers: per-side strikes/wing
width/badges, ORD-11 open/close instants, the recorded SPX reference, and
the D8b-fed Unmanaged P&L counterfactual (RPT-09a: every money figure comes
straight off reporting/folds.py's canonical entry_dollars/entry_credit_dollars).
"""
from datetime import date
from decimal import Decimal as D

from meic.adapters.occ import occ_symbol
from meic.domain.events import (
    CondorFilled,
    DayBrokerConfirmed,
    EntryClosed,
    EntryMarkSample,
    FilledLeg,
    SettlementRecorded,
    ShortStopped,
    SideClosed,
    SideExpired,
)
from meic.domain.projection import fold
from meic.reporting import day_table as dt

EXP = date(2026, 7, 9)


def _leg(right, role, strike, qty=1, price=D("1.00")):
    return FilledLeg(symbol=occ_symbol("SPXW", EXP, right, D(strike)), right=right,
                      role=role, qty=qty, price=price)


FULL_LEGS = (
    _leg("P", "short", "7535"),
    _leg("P", "long", "7510"),
    _leg("C", "short", "7540"),
    _leg("C", "long", "7565"),
)


def _entry(events):
    return fold(events).entries["2026-07-09#1"]


# --- wing_widths / side_strikes ---------------------------------------------

def test_wing_widths_from_recorded_legs():
    e = _entry([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    assert dt.wing_widths(e) == {"PUT": "25", "CALL": "25"}


def test_wing_widths_none_for_a_side_missing_a_leg():
    legs = (_leg("P", "short", "7535"), _leg("C", "short", "7540"), _leg("C", "long", "7565"))
    e = _entry([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=legs)])
    assert dt.wing_widths(e) == {"PUT": None, "CALL": "25"}


def test_side_strikes_from_recorded_legs():
    e = _entry([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    assert dt.side_strikes(e) == {
        "PUT": {"short": "7535", "long": "7510"},
        "CALL": {"short": "7540", "long": "7565"},
    }


# --- side_badge ---------------------------------------------------------------

def test_side_badge_protected_when_filled_and_untouched():
    e = _entry([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    assert dt.side_badge(e, "PUT") == "protected"
    assert dt.side_badge(e, "CALL") == "protected"


def test_side_badge_stopped_vs_decay_by_initiator():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.20"), slippage=D("0.05"),
                    initiator="resting_stop"),
        ShortStopped(entry_id="2026-07-09#1", side="CALL", fill=D("2.10"), slippage=D("0.02"),
                    initiator="decay"),
    ]
    e = _entry(events)
    assert dt.side_badge(e, "PUT") == "stopped"
    assert dt.side_badge(e, "CALL") == "decay"


def test_side_badge_expired_and_closed():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideClosed(entry_id="2026-07-09#1", side="CALL"),
    ]
    e = _entry(events)
    assert dt.side_badge(e, "PUT") == "expired"
    assert dt.side_badge(e, "CALL") == "closed"


def test_side_badge_whole_entry_close_covers_every_remaining_side():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual"),
    ]
    e = _entry(events)
    assert dt.side_badge(e, "PUT") == "closed"
    assert dt.side_badge(e, "CALL") == "closed"


def test_side_badge_whole_entry_decay_close():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        EntryClosed(entry_id="2026-07-09#1", initiator="decay"),
    ]
    e = _entry(events)
    assert dt.side_badge(e, "PUT") == "decay"


def test_stop_fill_count():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.20"), slippage=D("0.05")),
    ]
    assert dt.stop_fill_count(_entry(events)) == 1


# --- condor_filled_by_id / entry_close_at -------------------------------------

def test_condor_filled_by_id():
    cf = CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                      initiator="manual_entry", target_premium=D("3.50"))
    out = dt.condor_filled_by_id([cf])
    assert out["2026-07-09#1"] is cf
    assert out["2026-07-09#1"].initiator == "manual_entry"
    assert out["2026-07-09#1"].target_premium == D("3.50")


def test_entry_close_at_takes_the_latest_closing_event():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T13:32:00+00:00"),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.20"), slippage=D("0.05"),
                    at="2026-07-09T14:00:00+00:00"),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual",
                    at="2026-07-09T15:45:00+00:00"),
    ]
    assert dt.entry_close_at("2026-07-09#1", events) == "2026-07-09T15:45:00+00:00"


def test_entry_close_at_none_while_still_open():
    events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)]
    assert dt.entry_close_at("2026-07-09#1", events) is None


# --- recorded_spx --------------------------------------------------------------

def test_recorded_spx_latest_before_close():
    events = [
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T14:00:00+00:00", spot=D("7538")),
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T15:00:00+00:00", spot=D("7541")),
    ]
    spx = dt.recorded_spx("2026-07-09#1", events)
    assert spx.value == "7541" and spx.label == "latest"


def test_recorded_spx_close_once_a_16_00_et_sample_lands():
    events = [
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T14:00:00+00:00", spot=D("7538")),
        # 2026-07-09 20:00 UTC == 16:00 ET exactly.
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T20:00:00+00:00", spot=D("7550")),
    ]
    spx = dt.recorded_spx("2026-07-09#1", events)
    assert spx.value == "7550" and spx.label == "close"


def test_recorded_spx_none_when_no_sample_ever_carries_a_spot():
    assert dt.recorded_spx("2026-07-09#1", []) == dt.RecordedSpx(value=None, label=None)


# --- unmanaged_pnl (RPT-17 item 2 / D8b) --------------------------------------

def _condor_filled_entry(net_credit=D("3.60")):
    return CondorFilled(entry_id="2026-07-09#1", net_credit=net_credit, legs=FULL_LEGS)


def test_unmanaged_pnl_from_the_16_00_recorded_spread():
    events = [
        _condor_filled_entry(),
        EntryClosed(entry_id="2026-07-09#1", initiator="take_profit",
                    at="2026-07-09T14:00:00+00:00"),
        # 20:00 UTC == 16:00 ET -- the close sample.
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T20:00:00+00:00",
                        put_short_mid=D("0.50"), put_long_mid=D("0.05"),
                        call_short_mid=D("0.40"), call_long_mid=D("0.03")),
    ]
    e = _entry(events)
    # premium received = 3.60 * 100 = $360; spread at close = (0.50-0.05)+(0.40-0.03) = 0.82 -> $82
    result = dt.unmanaged_pnl(e, events)
    assert result.status == "ok"
    assert D(result.value) == D("360.00") - D("82.00")


def test_unmanaged_pnl_no_data_when_no_close_time_sample_at_all():
    events = [_condor_filled_entry(),
             EntryClosed(entry_id="2026-07-09#1", initiator="manual",
                        at="2026-07-09T14:00:00+00:00")]
    e = _entry(events)
    result = dt.unmanaged_pnl(e, events)
    assert result.status == "no_data" and result.value is None


def test_unmanaged_pnl_no_data_when_a_leg_mid_is_missing_even_at_close():
    events = [
        _condor_filled_entry(),
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T20:00:00+00:00",
                        put_short_mid=D("0.50"), put_long_mid=None,
                        call_short_mid=D("0.40"), call_long_mid=D("0.03")),
    ]
    e = _entry(events)
    result = dt.unmanaged_pnl(e, events)
    assert result.status == "no_data" and result.value is None


def test_unmanaged_pnl_ignores_samples_before_the_close():
    """A sample recorded before 16:00 ET must never stand in for the close --
    D10: no interpolation, ever."""
    events = [
        _condor_filled_entry(),
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T15:00:00+00:00",
                        put_short_mid=D("9"), put_long_mid=D("9"),
                        call_short_mid=D("9"), call_long_mid=D("9")),
    ]
    e = _entry(events)
    result = dt.unmanaged_pnl(e, events)
    assert result.status == "no_data" and result.value is None


# --- is_provisional -------------------------------------------------------------

def test_is_provisional_true_while_settlement_uncaptured_and_unreconciled():
    legs = FULL_LEGS
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=legs),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
    ]
    e = _entry(events)
    assert e.settlement_pending is True
    assert dt.is_provisional(e, events) is True


def test_is_provisional_false_once_settlement_captured():
    legs = FULL_LEGS
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=legs),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        SettlementRecorded(entry_id="2026-07-09#1", day="2026-07-09", at="2026-07-10T00:00:00+00:00",
                           symbol=legs[0].symbol, sub_type="Expiration", quantity=1,
                           price=None, value=D("0"), fee=D("0")),
        SettlementRecorded(entry_id="2026-07-09#1", day="2026-07-09", at="2026-07-10T00:00:00+00:00",
                           symbol=legs[2].symbol, sub_type="Expiration", quantity=1,
                           price=None, value=D("0"), fee=D("0")),
    ]
    e = _entry(events)
    assert dt.is_provisional(e, events) is False


def test_is_provisional_false_once_the_day_is_broker_reconciled():
    legs = FULL_LEGS
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=legs),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        DayBrokerConfirmed(date="2026-07-09", at="2026-07-10T00:00:00+00:00"),
    ]
    e = _entry(events)
    assert dt.is_provisional(e, events) is False
