"""reporting.folds — RPT-01/02 pure event-log folds."""
from decimal import Decimal as D

from meic.domain.events import (
    CondorFilled,
    DayArmed,
    EntryClosed,
    EntrySkipped,
    FilledLeg,
    SettlementRecorded,
    ShortStopped,
)
from meic.reporting.folds import (
    contracts_of,
    core_results,
    daily_net,
    day_snapshot,
    entries_by_day,
    entry_credit_dollars,
    entry_day,
    entry_dollars,
    entry_dollars_fees,
    entry_trading_fees_dollars,
    trading_days,
)


def test_entry_day_parses_the_id_prefix():
    assert entry_day("2026-07-09#1") == "2026-07-09"
    assert entry_day("2026-07-09#12") == "2026-07-09"


def test_trading_days_includes_armed_skipped_and_filled_days_only():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        EntrySkipped(date="2026-07-10", entry_number=1, reason="not_armed"),
        # 2026-07-11 is a disarmed flat day: no event of any kind -- excluded.
    ]
    assert trading_days(events) == ("2026-07-09", "2026-07-10")


def test_trading_days_is_empty_for_an_empty_log():
    assert trading_days([]) == ()


def test_contracts_of_reads_the_recorded_leg_quantity():
    leg = FilledLeg(symbol="SPXW  260709P05600000", right="P", role="short",
                     qty=3, price=D("2.00"))
    from meic.domain.projection import EntryProjection

    entry = EntryProjection(entry_id="e1", net_credit=D("2.00"), legs=(leg,))
    assert contracts_of(entry) == 3


def test_contracts_of_falls_back_to_one_with_no_legs():
    from meic.domain.projection import EntryProjection

    assert contracts_of(EntryProjection(entry_id="e1")) == 1


def test_entry_dollars_applies_the_contract_multiplier():
    leg = FilledLeg(symbol="SPXW  260709P05600000", right="P", role="short",
                     qty=2, price=D("2.00"))
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), legs=(leg,)),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.80"), slippage=D("0")),
    ]
    entry = entries_by_day(events)["2026-07-09"][0]
    # pnl = 4.00 - 3.80 = 0.20/share; dollars = 0.20 * 100 * 2 contracts = 40.00
    assert entry_dollars(entry) == D("40.00")
    assert entry_credit_dollars(entry) == D("800.00")  # 4.00 * 100 * 2


def test_daily_net_zero_fills_a_qualifying_day_with_no_fills():
    events = [EntrySkipped(date="2026-07-09", entry_number=1, reason="not_armed")]
    assert daily_net(events) == {"2026-07-09": D("0")}


def test_core_results_counts_skips_and_fees_and_premium_capture():
    events = [
        DayArmed(date="2026-07-09", entry_count=2),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), fee=D("1.00")),
        EntrySkipped(date="2026-07-09", entry_number=2, reason="incomplete_chain"),
    ]
    r = core_results(events)
    assert r.filled == 1
    assert r.skipped_by_reason == {"incomplete_chain": 1}
    assert r.fired == 2
    assert r.net_pnl == D("300.00")           # (4.00 - 1.00 fee) * 100
    assert r.gross_pnl == D("400.00")         # net + fees added back
    assert r.fees == D("100.00")              # 1.00 fee * 100 (same scale as net_credit)
    assert r.total_credit == D("400.00")
    assert r.premium_capture == r.net_pnl / r.total_credit
    assert r.day_win_rate == D("1")
    assert r.entry_win_rate == D("1")


def test_core_results_on_an_empty_log_has_no_rates():
    r = core_results([])
    assert r.filled == 0 and r.fired == 0
    assert r.day_win_rate is None
    assert r.entry_win_rate is None
    assert r.premium_capture is None


def test_day_snapshot_flat_when_every_entry_is_settled():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), fee=D("2.20")),
        EntryClosed(entry_id="2026-07-09#1", initiator="eod"),
    ]
    snap = day_snapshot(events, "2026-07-09")
    assert snap.flat is True
    assert snap.fees == D("220.00")
    assert snap.net == D("180.00")  # (4.00 - 2.20) * 100
    assert snap.fill_count == 1


def test_day_snapshot_not_flat_when_an_entry_is_still_open():
    events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    snap = day_snapshot(events, "2026-07-09")
    assert snap.flat is False


def test_day_snapshot_on_a_day_with_no_entries_is_flat_and_zero():
    snap = day_snapshot([], "2026-07-09")
    assert snap.flat is True and snap.fees == D("0") and snap.net == D("0") and snap.fill_count == 0


# --- RPT-16 settlement import (operator ruling 2026-07-10) --------------------

def _imported(symbol, action, *, price=None, fee=None, value=None, qty=1,
              order_id="482390058", day="2026-07-09"):
    from meic.domain.events import ExternalFillImported

    return ExternalFillImported(
        day=day, at=f"{day}T15:29:00-04:00", order_id=order_id, symbol=symbol,
        action=action, quantity=qty, price=price, fee=fee, value=value,
        imported_at="2026-07-10T09:00:00-04:00", source="tastytrade_history")


def test_imported_fill_dollars_uses_signed_value_directly_for_settlement_rows():
    """A settlement row's `value` is the broker's own NET cash effect in real
    dollars -- signed, already net of fee, NO x100 contract multiplier."""
    from meic.reporting.folds import imported_fill_dollars

    cash = _imported("SPXW  260709C07540000", "Cash Settled Assignment",
                     price=D("7540.0"), fee=D("5.00"), value=D("-369.00"))
    assert imported_fill_dollars(cash) == D("-369.00")

    zero = _imported("SPXW  260709P07535000", "Expiration", fee=D("0"), value=D("0"))
    assert imported_fill_dollars(zero) == D("0")


def test_imported_fill_dollars_keeps_price_x100_math_for_trade_rows():
    from meic.reporting.folds import imported_fill_dollars

    sell = _imported("SPXW  260709P07535000", "Sell to Open", price=D("2.20"), fee=D("1.22"))
    assert imported_fill_dollars(sell) == D("220.00")


def test_imported_day_net_is_minus_13_88_for_the_real_2026_07_09_day():
    """The ruling's acceptance criterion: entry credit 355.12 (3.60 credit
    x100 - 4.88 fees) plus the -369.00 settlement = -13.88; total fees
    4.88 + 5.00 = 9.88. The settlement's own fee must NOT be subtracted a
    second time (its value is already net-of-fee)."""
    from meic.reporting.folds import imported_day_fees, imported_day_net

    fills = (
        _imported("SPXW  260709P07535000", "Sell to Open", price=D("2.20"), fee=D("1.22")),
        _imported("SPXW  260709P07510000", "Buy to Open", price=D("0.40"), fee=D("1.22")),
        _imported("SPXW  260709C07540000", "Sell to Open", price=D("2.15"), fee=D("1.22")),
        _imported("SPXW  260709C07565000", "Buy to Open", price=D("0.35"), fee=D("1.22")),
        _imported("SPXW  260709C07540000", "Cash Settled Assignment",
                  price=D("7540.0"), fee=D("5.00"), value=D("-369.00")),
        _imported("SPXW  260709P07535000", "Expiration", fee=D("0"), value=D("0")),
        _imported("SPXW  260709P07510000", "Expiration", fee=D("0"), value=D("0")),
        _imported("SPXW  260709C07565000", "Expiration", fee=D("0"), value=D("0")),
    )
    assert imported_day_net(fills) == D("-13.88")
    assert imported_day_fees(fills) == D("9.88")


def test_daily_net_and_core_results_fold_the_settlement_into_the_day():
    events = [
        _imported("SPXW  260709C07540000", "Sell to Open", price=D("2.15"), fee=D("1.22")),
        _imported("SPXW  260709C07540000", "Cash Settled Assignment",
                  price=D("7540.0"), fee=D("5.00"), value=D("-369.00")),
    ]
    # 2.15*100 - 1.22 = 213.78; 213.78 - 369.00 = -155.22
    assert daily_net(events) == {"2026-07-09": D("-155.22")}
    r = core_results(events)
    assert r.net_pnl == D("-155.22")
    assert r.imported_net == D("-155.22")
    assert r.imported_fees == D("6.22")
    assert r.imported_fills == 2
    assert r.gross_pnl == r.net_pnl + r.fees  # fee add-back identity holds


# --- EOD-01 v1.59: LIVE settlement capture folds into P&L ---------------------

def _real_condor_legs():
    return (
        FilledLeg(symbol="SPXW  260709P07535000", right="P", role="short", qty=1, price=D("2.20")),
        FilledLeg(symbol="SPXW  260709P07510000", right="P", role="long", qty=1, price=D("0.40")),
        FilledLeg(symbol="SPXW  260709C07540000", right="C", role="short", qty=1, price=D("2.15")),
        FilledLeg(symbol="SPXW  260709C07565000", right="C", role="long", qty=1, price=D("0.35")),
    )


def _real_settlement_events(entry_id):
    at = "2026-07-10T02:00:00+00:00"
    return [
        SettlementRecorded(entry_id=entry_id, day="2026-07-09", at=at,
                           symbol="SPXW  260709C07540000", sub_type="Cash Settled Assignment",
                           quantity=1, price=D("7540.0"), value=D("-369.00"), fee=D("5.00")),
        SettlementRecorded(entry_id=entry_id, day="2026-07-09", at=at,
                           symbol="SPXW  260709P07535000", sub_type="Expiration",
                           quantity=1, price=None, value=D("0"), fee=D("0")),
        SettlementRecorded(entry_id=entry_id, day="2026-07-09", at=at,
                           symbol="SPXW  260709P07510000", sub_type="Expiration",
                           quantity=1, price=None, value=D("0"), fee=D("0")),
        SettlementRecorded(entry_id=entry_id, day="2026-07-09", at=at,
                           symbol="SPXW  260709C07565000", sub_type="Expiration",
                           quantity=1, price=None, value=D("0"), fee=D("0")),
    ]


def test_pinned_2026_07_09_vector_nets_minus_13_88_once_settled():
    """The operator's acceptance criterion for EOD-01 v1.59: 4 legs netting
    +355.12, a -369.00 broker settlement on the short C7540 -> true net
    -13.88. Total fees 4.88 (entry) + 5.00 (settlement) = 9.88."""
    entry_id = "2026-07-09#1"
    events = [
        CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.0488"),
                    legs=_real_condor_legs()),
        *_real_settlement_events(entry_id),
    ]
    entry = entries_by_day(events)["2026-07-09"][0]
    assert entry_dollars(entry) == D("-13.88")
    assert entry_dollars_fees(entry) == D("9.88")
    assert entry_trading_fees_dollars(entry) == D("4.88")  # excludes the settlement's own fee
    assert entry.settlement_pending is False  # every unstopped short's symbol is captured


def test_day_snapshot_stamps_flat_only_once_every_short_is_settled():
    """The live path emits no SideExpired at all -- EOD-01 v1.59's
    settlement capture is what makes a held-to-expiry day 'flat' for RPT-15."""
    entry_id = "2026-07-09#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.0488"),
                           legs=_real_condor_legs())]
    assert day_snapshot(events, "2026-07-09").flat is False  # settlement not captured yet

    events.extend(_real_settlement_events(entry_id))
    snap = day_snapshot(events, "2026-07-09")
    assert snap.flat is True
    assert snap.net == D("-13.88")
    assert snap.fees == D("9.88")
