"""reporting.folds — RPT-01/02 pure event-log folds."""
from decimal import Decimal as D

from meic.domain.events import (
    CondorFilled,
    DayArmed,
    EntryClosed,
    EntrySkipped,
    FilledLeg,
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
