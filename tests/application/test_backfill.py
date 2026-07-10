"""application.backfill — RPT-16 one-time historical backfill (proposed
amendment, AMENDMENT-PROPOSAL-historical-backfill.md). Fakes a broker
Transaction with the SDK's real field names (order_id/symbol/action/
quantity/price/regulatory_fees/clearing_fees/commission/
proprietary_index_option_fees/executed_at) rather than importing the SDK.
Async calls are driven with `asyncio.run(...)` inside plain `def test_...`
functions -- the same convention as tests/application/test_entry_pipeline.py,
never a pytest-asyncio marker.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from meic.application.backfill import backfill_day
from meic.domain.events import ExternalFillImported


@dataclass
class FakeTransaction:
    """Mirrors the tastytrade SDK's `Transaction` shape closely enough for
    backfill_day's field reads -- only the fields the service touches."""
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


class FakeBrokerReads:
    def __init__(self, fills: list[FakeTransaction]) -> None:
        self._fills = fills
        self.calls: list[str] = []

    async def day_fills(self, day: str):
        self.calls.append(day)
        return self._fills


def _now_iso() -> str:
    return "2026-07-10T09:00:00-04:00"


def test_imports_only_the_supplied_order_ids_and_counts_foreign():
    fills = [
        FakeTransaction(order_id=482214732, symbol="SPXW  260709P05600000",
                        action="Sell to Open", quantity=D("1"), price=D("3.00"),
                        executed_at=datetime(2026, 7, 9, 14, 31, tzinfo=timezone.utc),
                        regulatory_fees=D("-0.01"), clearing_fees=D("-0.02")),
        FakeTransaction(order_id=482147293, symbol="SPXW  260709P05500000",
                        action="Sell to Open", quantity=D("1"), price=D("2.50"),
                        executed_at=datetime(2026, 7, 9, 14, 32, tzinfo=timezone.utc)),
        # No order_id at all -- e.g. a settlement-style Trade row: also foreign.
        FakeTransaction(order_id=None, symbol=None, action=None, quantity=None,
                        price=None, executed_at=datetime(2026, 7, 9, 16, 15, tzinfo=timezone.utc)),
    ]
    events: list = []
    broker = FakeBrokerReads(fills)
    result = asyncio.run(backfill_day(events, broker, "2026-07-09", {"482214732"}, now_iso=_now_iso))

    assert result == {"result": "imported", "fills": 1, "skipped_foreign": 2}
    imported = [e for e in events if isinstance(e, ExternalFillImported)]
    assert len(imported) == 1
    assert imported[0].order_id == "482214732"
    assert imported[0].symbol == "SPXW  260709P05600000"
    assert imported[0].action == "Sell to Open"
    assert imported[0].day == "2026-07-09"
    assert imported[0].source == "tastytrade_history"
    assert imported[0].imported_at == "2026-07-10T09:00:00-04:00"


def test_reimport_is_a_no_op():
    events: list = [ExternalFillImported(
        day="2026-07-09", at="t", order_id="482214732", symbol="X", action="Sell to Open",
        quantity=1, price=D("3.00"), fee=D("0.03"), imported_at="t", source="tastytrade_history")]
    broker = FakeBrokerReads([FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=D("3.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))])

    result = asyncio.run(backfill_day(events, broker, "2026-07-09", {"482214732"}, now_iso=_now_iso))

    assert result == {"result": "already_imported", "count": 1}
    assert broker.calls == []  # never even fetches -- idempotent, no redundant broker read
    assert len([e for e in events if isinstance(e, ExternalFillImported)]) == 1


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
    broker = FakeBrokerReads(fills)
    asyncio.run(backfill_day(events, broker, "2026-07-09", {"482214732"}, now_iso=_now_iso))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.fee == D("0.70")


def test_no_fee_data_at_all_is_none_not_fabricated_zero():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=D("3.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    broker = FakeBrokerReads(fills)
    asyncio.run(backfill_day(events, broker, "2026-07-09", {"482214732"}, now_iso=_now_iso))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.fee is None


def test_no_price_is_none_not_fabricated_zero():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=None,
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    broker = FakeBrokerReads(fills)
    asyncio.run(backfill_day(events, broker, "2026-07-09", {"482214732"}, now_iso=_now_iso))

    imported = [e for e in events if isinstance(e, ExternalFillImported)][0]
    assert imported.price is None


def test_order_ids_matched_regardless_of_int_or_str_supplied():
    fills = [FakeTransaction(
        order_id=482214732, symbol="X", action="Sell to Open", quantity=D("1"), price=D("3.00"),
        executed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))]
    events: list = []
    broker = FakeBrokerReads(fills)
    result = asyncio.run(backfill_day(events, broker, "2026-07-09", {482214732}, now_iso=_now_iso))

    assert result["fills"] == 1
