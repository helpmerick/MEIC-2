"""OWN-03 / RPT-16 escape hatch (2026-07-12): a day whose entries predate
order-id journaling (the REAL 2026-07-10 incident -- `CondorFilled` carried
no `broker_order_id` at all) can be made attributable by backfilling the
bot's real broker order ids via `OwnOrderIdBackfilled`
(`application/backfill_order_ids.backfill_own_order_ids`), the RPT-16
operator-supplied-order-id escape hatch.

`ReportReconciler.reconcile_day` must:
  1. no longer refuse the day as "unattributable" once a backfilled ENTRY id
     exists for it;
  2. scope `own_order_ids` to the three backfilled ids (entry/stop/lex) the
     same way it already scopes a directly-journaled `CondorFilled`/
     `StopPlaced`/`LexOrderPlaced`;
  3. exclude every foreign row (the operator's own 7580 condor, order
     482759560, and Micro-Nasdaq futures put, order 482542569) exactly as
     the existing OWN-01/OWN-03 fix already does for a directly-journaled day
     (see test_report_reconciler_own_scoping.py);
  4. compute cash_delta = 43.68 and fees = 6.32 from the bot's own 6 rows,
     and -- because the bot's OWN pre-fix fold is the wrong $40.00/$0.00 the
     real incident produced -- emit `scope="own"` CorrectionRecords carrying
     the true 43.68/6.32 broker values.
"""
from __future__ import annotations

import asyncio
import types
from decimal import Decimal as D

from meic.application.backfill_order_ids import backfill_own_order_ids
from meic.application.report_reconciler import ReportReconciler
from meic.domain.events import CondorFilled, CorrectionRecord, DayBrokerConfirmed, EntryClosed

DAY = "2026-07-10"
ENTRY_ID = f"{DAY}#1"

ENTRY_ORDER_ID = "482621396"     # role "entry": the 4-leg condor OPEN
STOP_ORDER_ID = "482621556"      # role "stop": Buy-to-Close C7565 @ 4.80
LEX_ORDER_ID = "482760202"       # role "lex": Sell-to-Close C7595 @ 0.10

FOREIGN_CONDOR_ORDER_ID = "482759560"   # operator's own 7580 condor
FOREIGN_MNQ_ORDER_ID = "482542569"      # operator's own MNQ futures put

TRUE_CASH_DELTA = D("43.68")
TRUE_FEES = D("6.32")


def _row(*, order_id=None, symbol, value, net_value, **extra):
    return types.SimpleNamespace(order_id=order_id, symbol=symbol, value=value,
                                 net_value=net_value, **extra)


def _own_rows():
    """The exact real 2026-07-10 own rows (operator-verified), per the
    backfill task's ratified vector: sum(net_value) == 43.68,
    sum(value - net_value) == 6.32."""
    return [
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710C07565000",
             value=D("313.00"), net_value=D("311.78")),   # open C7565 Sell
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710C07595000",
             value=D("-17.00"), net_value=D("-18.22")),   # open C7595 Buy
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710P07535000",
             value=D("301.00"), net_value=D("299.78")),   # open P7535 Sell
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710P07505000",
             value=D("-77.00"), net_value=D("-78.22")),   # open P7505 Buy
        _row(order_id=STOP_ORDER_ID, symbol="SPXW  260710C07565000",
             value=D("-480.00"), net_value=D("-480.72")),  # stop C7565 Buy
        _row(order_id=LEX_ORDER_ID, symbol="SPXW  260710C07595000",
             value=D("10.00"), net_value=D("9.28")),       # lex C7595 Sell
    ]


def _foreign_condor_rows():
    """The operator's OWN 7580 condor -- a different order id, must never
    enter the bot's ledger."""
    return [
        _row(order_id=FOREIGN_CONDOR_ORDER_ID, symbol="SPXW  260710C07580000",
             value=D("250.00"), net_value=D("248.78")),
        _row(order_id=FOREIGN_CONDOR_ORDER_ID, symbol="SPXW  260710C07610000",
             value=D("-90.00"), net_value=D("-91.22")),
        _row(order_id=FOREIGN_CONDOR_ORDER_ID, symbol="SPXW  260710P07550000",
             value=D("240.00"), net_value=D("238.78")),
        _row(order_id=FOREIGN_CONDOR_ORDER_ID, symbol="SPXW  260710P07520000",
             value=D("-100.00"), net_value=D("-101.22")),
    ]


def _foreign_mnq_fill():
    return _row(order_id=FOREIGN_MNQ_ORDER_ID, symbol="./MNQU26P20500",
               value=D("-926.00"), net_value=D("-928.26"))


def _foreign_condor_settlement():
    return _row(symbol="SPXW  260710C07580000", value=D("-461.00"), net_value=D("-466.00"),
               transaction_sub_type="Assignment", price=D("7580.0"), quantity=D("1"))


class _SharedAccountBroker:
    def __init__(self, fills, settlements):
        self._fills, self._settlements = fills, settlements

    async def positions(self):
        return []

    async def day_fills(self, day):
        return list(self._fills)

    async def day_settlements(self, day):
        return list(self._settlements)

    async def cash_and_fees(self, day):
        raise AssertionError("RPT-15 must never read the whole-account aggregate")


def _legacy_bot_log():
    """The REAL 2026-07-10 pre-fix journal shape: `CondorFilled` carries NO
    broker_order_id. Its bot-side fold is the WRONG $40.00/$0.00 the
    dashboard showed before this fix -- deliberately NOT 43.68/6.32, so the
    reconcile below must actually correct it, not just confirm it."""
    return [
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("0.40"), fee=D("0")),
        EntryClosed(entry_id=ENTRY_ID, initiator="eod"),
    ]


def _backfill(events):
    return backfill_own_order_ids(
        events, ENTRY_ID,
        [(ENTRY_ORDER_ID, "entry"), (STOP_ORDER_ID, "stop"), (LEX_ORDER_ID, "lex")],
        at="2026-07-12T09:00:00-04:00", note="operator-authorised backfill, RPT-16")


def test_a_backfilled_day_is_no_longer_unattributable():
    events = _legacy_bot_log()
    _backfill(events)
    broker = _SharedAccountBroker(fills=_own_rows(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status != "unattributable"


def test_backfilled_ids_are_picked_up_by_own_order_ids():
    from meic.reporting.own_orders import own_order_ids

    events = _legacy_bot_log()
    _backfill(events)

    assert own_order_ids(events) == {ENTRY_ORDER_ID, STOP_ORDER_ID, LEX_ORDER_ID}


def test_real_end_to_end_vector_computes_true_cash_and_fees_excluding_foreign_rows():
    events = _legacy_bot_log()
    _backfill(events)

    fills = _own_rows() + _foreign_condor_rows() + [_foreign_mnq_fill()]
    settlements = [_foreign_condor_settlement()]
    broker = _SharedAccountBroker(fills=fills, settlements=settlements)
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-12T09:05:00-04:00")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    # The bot's own pre-fix fold (40.00/0.00) disagrees with broker truth, so
    # this day corrects -- it does NOT silently confirm on the wrong number.
    assert outcome.status == "corrected"
    assert outcome.ambiguous_settlements == 0

    corrections = {c.field: c for c in outcome.corrections}
    assert set(corrections) == {"cash_delta", "fees"}

    cash = corrections["cash_delta"]
    assert D(cash.broker_value) == TRUE_CASH_DELTA
    assert D(cash.bot_value) == D("40.00")
    assert cash.scope == "own"

    fees = corrections["fees"]
    assert D(fees.broker_value) == TRUE_FEES
    assert fees.scope == "own"

    # Every appended CorrectionRecord in the log agrees -- no foreign row
    # (the -928.26 MNQ put, the -466.00 settlement, or the 7580 condor)
    # leaked into either figure.
    for e in events:
        if isinstance(e, CorrectionRecord) and e.date == DAY:
            assert e.scope == "own"
            if e.field == "cash_delta":
                assert D(e.broker_value) == TRUE_CASH_DELTA
            if e.field == "fees":
                assert D(e.broker_value) == TRUE_FEES

    assert not any(isinstance(e, DayBrokerConfirmed) and e.date == DAY for e in events)


def test_fill_count_still_counts_one_entry_not_six_broker_rows():
    events = _legacy_bot_log()
    _backfill(events)
    broker = _SharedAccountBroker(fills=_own_rows(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    fill_count_correction = next(
        (c for c in outcome.corrections if c.field == "fill_count"), None)
    # Either it agrees outright, or -- if present -- the broker side must
    # still read "1" (one entry order), never "6" (six broker leg rows).
    if fill_count_correction is not None:
        assert fill_count_correction.broker_value == "1"
