"""application.backfill — RPT-16 one-time historical backfill (proposed
amendment, AMENDMENT-PROPOSAL-historical-backfill.md, settlement import per
operator ruling 2026-07-10). Fakes a broker Transaction with the SDK's real
field names (order_id/symbol/action/quantity/price/executed_at/value/
net_value/transaction_type/transaction_sub_type/regulatory_fees/
clearing_fees/commission/proprietary_index_option_fees) rather than
importing the SDK. Async calls are driven with `asyncio.run(...)` inside
plain `def test_...` functions -- the same convention as
tests/application/test_entry_pipeline.py, never a pytest-asyncio marker.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from meic.application.backfill import backfill_day
from meic.domain.events import ExternalFillImported
from meic.reporting.folds import imported_day_fees, imported_day_net


@dataclass
class FakeTransaction:
    """Mirrors the tastytrade SDK's `Transaction` shape closely enough for
    backfill_day's field reads -- only the fields the service touches.
    Trade rows leave value/net_value at None (backfill never reads them for
    a Trade fill); Receive-Deliver settlement rows carry both, exactly as
    the real SDK does (verified live 2026-07-10: "Cash Settled Assignment"
    value=-364.0 net_value=-369.0 price=7540.0 on the settled symbol;
    "Expiration"/"Assignment" removal rows value 0)."""
    order_id: int | None
    symbol: str | None
    action: str | None
    quantity: D | None
    price: D | None
    executed_at: datetime
    regulatory_fees: D | None = None
    clearing_fees: D | None = None
    commission: D | None = None
    proprietary_index_option_fees: D | None = None
    transaction_type: str = "Trade"
    transaction_sub_type: str | None = None
    value: D | None = None
    net_value: D | None = None


class FakeBrokerReads:
    def __init__(self, fills: list[FakeTransaction],
                 settlements: list[FakeTransaction] | None = None) -> None:
        self._fills = fills
        self._settlements = settlements if settlements is not None else []
        self.calls: list[tuple[str, str]] = []

    async def day_fills(self, day: str):
        self.calls.append(("day_fills", day))
        return self._fills

    async def day_settlements(self, day: str):
        self.calls.append(("day_settlements", day))
        return self._settlements


def _now_iso() -> str:
    return "2026-07-10T09:00:00-04:00"


def _run(events, broker, order_ids={"482214732"}, day="2026-07-09"):
    return asyncio.run(backfill_day(events, broker, day, order_ids, now_iso=_now_iso))


# --- The exact real 2026-07-09 shapes (order 482390058, held to expiry) ------

_AT = datetime(2026, 7, 9, 19, 29, tzinfo=timezone.utc)  # 15:29 ET fill time
_SETTLE_AT = datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc)  # posts after the bell

P7535 = "SPXW  260709P07535000"
P7510 = "SPXW  260709P07510000"
C7540 = "SPXW  260709C07540000"
C7565 = "SPXW  260709C07565000"


def _real_trade_legs() -> list[FakeTransaction]:
    """Four legs, broker-actual net credit 3.60, fees 4.88 total -->
    entry contribution (2.20 - 0.40 + 2.15 - 0.35)*100 - 4.88 = 355.12."""
    def leg(symbol, action, price):
        return FakeTransaction(
            order_id=482390058, symbol=symbol, action=action, quantity=D("1"),
            price=price, executed_at=_AT,
            regulatory_fees=D("-0.04"), clearing_fees=D("-0.10"),
            commission=D("-1.00"), proprietary_index_option_fees=D("-0.08"))
    return [
        leg(P7535, "Sell to Open", D("2.20")),
        leg(P7510, "Buy to Open", D("0.40")),
        leg(C7540, "Sell to Open", D("2.15")),
        leg(C7565, "Buy to Open", D("0.35")),
    ]


def _real_settlements() -> list[FakeTransaction]:
    """The C7540 cash-settled assignment (-364 value, -369 net = $5 fee) plus
    the three worthless legs' zero-value Expiration removals."""
    def settle(symbol, sub_type, value, net_value, price=None):
        return FakeTransaction(
            order_id=None, symbol=symbol, action=None, quantity=D("1"),
            price=price, executed_at=_SETTLE_AT,
            transaction_type="Receive Deliver", transaction_sub_type=sub_type,
            value=value, net_value=net_value)
    return [
        settle(C7540, "Cash Settled Assignment", D("-364.0"), D("-369.0"), price=D("7540.0")),
        settle(P7535, "Expiration", D("0"), D("0")),
        settle(P7510, "Expiration", D("0"), D("0")),
        settle(C7565, "Expiration", D("0"), D("0")),
    ]


# --- OWN-03 / Trade-fill matching (unchanged behavior) ------------------------

def test_imports_only_the_supplied_order_ids_and_counts_foreign():
    fills = [
        FakeTransaction(order_id=482214732, symbol="SPXW  260709P05600000",
                        action="Sell to Open", quantity=D("1"), price=D("3.00"),
                        executed_at=datetime(2026, 7, 9, 14, 31, tzinfo=timezone.utc),
                        regulatory_fees=D("-0.01"), clearing_fees=D("-0.02")),
        FakeTransaction(order_id=482147293, symbol="SPXW  260709P05500000",
                        action="Sell to Open", quantity=D("1"), price=D("2.50"),
                        executed_at=datetime(2026, 7, 9, 14, 32, tzinfo=timezone.utc)),
        # No order_id at all on a Trade row: also foreign.
        FakeTransaction(order_id=None, symbol=None, action=None, quantity=None,
                        price=None, executed_at=datetime(2026, 7, 9, 16, 15, tzinfo=timezone.utc)),
    ]
    events: list = []
    result = _run(events, FakeBrokerReads(fills))

    assert result == {"result": "imported", "fills": 1, "skipped_foreign": 2,
                      "settlements": 0, "ambiguous_settlements": 0}
    imported = [e for e in events if isinstance(e, ExternalFillImported)]
    assert len(imported) == 1
    assert imported[0].order_id == "482214732"
    assert imported[0].symbol == "SPXW  260709P05600000"
    assert imported[0].action == "Sell to Open"
    assert imported[0].day == "2026-07-09"
    assert imported[0].value is None  # Trade-style row: never a settlement value
    assert imported[0].source == "tastytrade_history"
    assert imported[0].imported_at == "2026-07-10T09:00:00-04:00"


def test_fee_total_sums_the_four_fee_fields_as_a_positive_cost():
    """The SDK's set_sign_for validator makes these fields NEGATIVE
    (debit-effect); backfill_day must record the POSITIVE cost, matching
    this codebase's existing fee convention (e.g. ShortStopped.fee)."""
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Buy to Close", quantity=D("2"), price=D("1.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        regulatory_fees=D("-0.01"), clearing_fees=D("-0.02"), commission=D("-0.65"),
        proprietary_index_option_fees=D("-0.02"))]
    events: list = []
    _run(events, FakeBrokerReads(fills))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.fee == D("0.70")


def test_no_fee_data_at_all_is_none_not_fabricated_zero():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=D("3.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    _run(events, FakeBrokerReads(fills))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.fee is None


def test_no_price_is_none_not_fabricated_zero():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=None,
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    _run(events, FakeBrokerReads(fills))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.price is None


def test_order_ids_matched_regardless_of_int_or_str_supplied():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=D("3.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    result = _run(events, FakeBrokerReads(fills), order_ids={482214732})

    assert result["fills"] == 1


# --- Settlement import (operator ruling 2026-07-10) ---------------------------

def test_settlement_import_records_the_exact_real_2026_07_09_shapes():
    """The C7540 cash-settled assignment: action = the sub_type string,
    value = signed net_value (-369.00), fee = |net_value - value| = 5.00,
    price = the settle strike reference. The three worthless legs' zero
    Expiration rows import too (terminal-state documentation, zero cash)."""
    events: list = []
    result = _run(events, FakeBrokerReads(_real_trade_legs(), _real_settlements()),
                  order_ids={"482390058"})

    assert result == {"result": "imported", "fills": 4, "skipped_foreign": 0,
                      "settlements": 4, "ambiguous_settlements": 0}
    settlement_rows = [e for e in events
                       if isinstance(e, ExternalFillImported) and e.value is not None]
    assert len(settlement_rows) == 4
    cash = next(e for e in settlement_rows if e.action == "Cash Settled Assignment")
    assert cash.symbol == C7540
    assert cash.value == D("-369.0")
    assert cash.fee == D("5.0")
    assert cash.price == D("7540.0")
    assert cash.quantity == 1
    assert cash.order_id == "482390058"  # attributed to the owning order by symbol
    zeros = [e for e in settlement_rows if e.action == "Expiration"]
    assert {e.symbol for e in zeros} == {P7535, P7510, C7565}
    assert all(e.value == D("0") and e.fee == D("0") for e in zeros)


def test_settlement_import_makes_the_day_a_13_88_loss_not_a_355_12_win():
    """The operator ruling's acceptance criterion: entry credit 355.12 minus
    the -369.00 settlement = day net -13.88; fees 4.88 (entry) + 5.00
    (settlement) = 9.88."""
    events: list = []
    _run(events, FakeBrokerReads(_real_trade_legs(), _real_settlements()),
         order_ids={"482390058"})

    fills = tuple(e for e in events if isinstance(e, ExternalFillImported))
    assert imported_day_net(fills) == D("-13.88")
    assert imported_day_fees(fills) == D("9.88")


def test_settlement_for_a_symbol_we_never_traded_is_ignored():
    """A Receive-Deliver row whose symbol matches none of OUR imported
    orders' fills (e.g. the operator's own expiring position) is simply not
    ours -- never imported, never counted ambiguous."""
    foreign_settle = FakeTransaction(
        order_id=None, symbol="SPXW  260709P05500000", action=None, quantity=D("1"),
        price=None, executed_at=_SETTLE_AT, transaction_type="Receive Deliver",
        transaction_sub_type="Expiration", value=D("0"), net_value=D("0"))
    events: list = []
    result = _run(events, FakeBrokerReads(_real_trade_legs(), [foreign_settle]),
                  order_ids={"482390058"})

    assert result["settlements"] == 0
    assert result["ambiguous_settlements"] == 0
    assert all(e.value is None for e in events if isinstance(e, ExternalFillImported))


def test_ambiguous_settlement_symbol_shared_with_foreign_fills_is_skipped_and_counted():
    """OWN-03 guard: when a settlement symbol was ALSO traded by a foreign
    order the same day, its cash is unattributable from broker data alone --
    skip it and surface the count, never guess."""
    trade_legs = _real_trade_legs()
    foreign_same_symbol = FakeTransaction(
        order_id=999999999, symbol=C7540, action="Sell to Open", quantity=D("2"),
        price=D("2.10"), executed_at=_AT)
    events: list = []
    result = _run(events, FakeBrokerReads(trade_legs + [foreign_same_symbol],
                                          _real_settlements()),
                  order_ids={"482390058"})

    assert result["fills"] == 4
    assert result["skipped_foreign"] == 1
    # C7540's cash assignment is ambiguous; the three unshared Expirations import.
    assert result["settlements"] == 3
    assert result["ambiguous_settlements"] == 1
    assert not any(e.action == "Cash Settled Assignment"
                   for e in events if isinstance(e, ExternalFillImported))


# --- Transaction-level idempotency (REWORKED 2026-07-10) -----------------------

def test_rerun_after_fills_only_import_adds_exactly_the_settlements():
    """The scenario that forced the rework: the first import ran before the
    settlements posted (fills only). Re-running once they exist must ADD the
    settlement rows -- and nothing else."""
    events: list = []
    broker_before = FakeBrokerReads(_real_trade_legs(), [])  # settlements not posted yet
    r1 = _run(events, broker_before, order_ids={"482390058"})
    assert r1["fills"] == 4 and r1["settlements"] == 0
    assert len(events) == 4

    broker_after = FakeBrokerReads(_real_trade_legs(), _real_settlements())
    r2 = _run(events, broker_after, order_ids={"482390058"})
    assert r2 == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                  "settlements": 4, "ambiguous_settlements": 0}
    assert len(events) == 8

    r3 = _run(events, broker_after, order_ids={"482390058"})
    assert r3 == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                  "settlements": 0, "ambiguous_settlements": 0}
    assert len(events) == 8  # third run: a true no-op, nothing appended


def test_reimport_of_a_fully_imported_day_appends_nothing():
    events: list = []
    broker = FakeBrokerReads(_real_trade_legs(), _real_settlements())
    _run(events, broker, order_ids={"482390058"})
    before = list(events)

    result = _run(events, broker, order_ids={"482390058"})
    assert result == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                      "settlements": 0, "ambiguous_settlements": 0}
    assert events == before


def test_idempotency_is_scoped_per_day():
    """An identical-looking fill recorded for ANOTHER day never blocks this
    day's import -- the existing-key set is built from `day`'s events only."""
    other_day = ExternalFillImported(
        day="2026-07-08", at=_AT.isoformat(), order_id="482390058", symbol=P7535,
        action="Sell to Open", quantity=1, price=D("2.20"), fee=D("1.22"),
        imported_at="t", source="tastytrade_history")
    events: list = [other_day]
    result = _run(events, FakeBrokerReads(_real_trade_legs(), []), order_ids={"482390058"})
    assert result["fills"] == 4
