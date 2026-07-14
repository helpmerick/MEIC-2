"""OWN-01 append-only retraction (2026-07-14, operator ruling): the REAL
2026-07-10 CALL-side incident this slice exists for. The operator's own
order 482760202 (`SPXW 260710C07595000`, sold from his own platform to
rescue the orphaned long after the bot's CALL short stopped with no live
stop-fill detection yet) was mistakenly journaled as
`OwnOrderIdBackfilled(role="lex")` for the bot's entry via a prior RPT-16
backfill (see test_report_reconciler_backfill_attribution.py, whose
`_own_rows()`/ids this test reuses).

After `OwnOrderIdRetracted` withdraws that id:
  1. `own_order_ids` no longer names it (tests/reporting/test_own_orders.py
     pins the generic trap; this file pins the end-to-end reconcile effect).
  2. its fill row (order 482760202, value 10.00, net_value 9.28, fee 0.72)
     no longer counts toward the bot's own cash_delta/fees.
  3. a FRESH reconcile appends NEW own-scoped CorrectionRecords carrying the
     corrected 34.40/5.60 (43.68 - 9.28, 6.32 - 0.72) -- and because the
     event log is append-only, the earlier (polluted) 43.68/6.32
     own-scoped records from BEFORE the retraction are still in the log,
     unchanged. `reporting/corrections.corrected_value` must render the
     NEWEST own-scoped record for (day, field), i.e. the corrected one.
"""
from __future__ import annotations

import asyncio
import types
from decimal import Decimal as D

from meic.application.backfill_order_ids import backfill_own_order_ids
from meic.application.report_reconciler import ReportReconciler
from meic.application.retract_own_order_id import retract_own_order_ids
from meic.domain.events import CorrectionRecord
from meic.reporting.corrections import corrected_value
from meic.reporting.own_orders import own_order_ids

DAY = "2026-07-10"
ENTRY_ID = f"{DAY}#1"

ENTRY_ORDER_ID = "482621396"
STOP_ORDER_ID = "482621556"
LEX_ORDER_ID = "482760202"   # the operator's own order, mistakenly backfilled then retracted

TRUE_CASH_DELTA = D("43.68")
TRUE_FEES = D("6.32")
CORRECTED_CASH_DELTA = D("34.40")   # 43.68 - 9.28 (the retracted row's net_value)
CORRECTED_FEES = D("5.60")          # 6.32 - 0.72 (the retracted row's fee)


def _row(*, order_id=None, symbol, value, net_value, **extra):
    return types.SimpleNamespace(order_id=order_id, symbol=symbol, value=value,
                                 net_value=net_value, **extra)


def _own_rows():
    return [
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710C07565000",
             value=D("313.00"), net_value=D("311.78")),
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710C07595000",
             value=D("-17.00"), net_value=D("-18.22")),
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710P07535000",
             value=D("301.00"), net_value=D("299.78")),
        _row(order_id=ENTRY_ORDER_ID, symbol="SPXW  260710P07505000",
             value=D("-77.00"), net_value=D("-78.22")),
        _row(order_id=STOP_ORDER_ID, symbol="SPXW  260710C07565000",
             value=D("-480.00"), net_value=D("-480.72")),
        _row(order_id=LEX_ORDER_ID, symbol="SPXW  260710C07595000",
             value=D("10.00"), net_value=D("9.28")),   # the operator's own rescue sale
    ]


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
    from meic.domain.events import CondorFilled, EntryClosed
    return [
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("0.40"), fee=D("0")),
        EntryClosed(entry_id=ENTRY_ID, initiator="eod"),
    ]


def _backfilled_log():
    events = _legacy_bot_log()
    backfill_own_order_ids(
        events, ENTRY_ID,
        [(ENTRY_ORDER_ID, "entry"), (STOP_ORDER_ID, "stop"), (LEX_ORDER_ID, "lex")],
        at="2026-07-12T09:00:00-04:00", note="operator-authorised backfill, RPT-16")
    return events


def test_retracted_id_no_longer_in_own_scope():
    events = _backfilled_log()
    assert LEX_ORDER_ID in own_order_ids(events)

    retract_own_order_ids(
        events, ENTRY_ID, [(LEX_ORDER_ID, "operator's own out-of-band order, not the bot's")],
        at="2026-07-14T09:00:00-04:00", note="operator ruling 2026-07-14, strict OWN-01")

    assert LEX_ORDER_ID not in own_order_ids(events)
    # the OTHER two ids on the same entry are unaffected
    assert own_order_ids(events) == {ENTRY_ORDER_ID, STOP_ORDER_ID}


def test_reconcile_before_retraction_pollutes_with_the_operators_rescue_sale():
    """Baseline: BEFORE retraction, the pre-existing behaviour (pinned by
    test_report_reconciler_backfill_attribution.py) counts the operator's
    own rescue sale as the bot's -- 43.68/6.32, not the true 34.40/5.60."""
    events = _backfilled_log()
    broker = _SharedAccountBroker(fills=_own_rows(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-12T09:05:00-04:00")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    assert outcome.status == "corrected"
    corrections = {c.field: c for c in outcome.corrections}
    assert D(corrections["cash_delta"].broker_value) == TRUE_CASH_DELTA
    assert D(corrections["fees"].broker_value) == TRUE_FEES


def test_reconcile_after_retraction_excludes_the_operators_fill_and_fee():
    events = _backfilled_log()
    retract_own_order_ids(
        events, ENTRY_ID, [(LEX_ORDER_ID, "operator's own out-of-band order, not the bot's")],
        at="2026-07-14T09:00:00-04:00", note="operator ruling 2026-07-14, strict OWN-01")

    broker = _SharedAccountBroker(fills=_own_rows(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-14T09:10:00-04:00")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    assert outcome.status == "corrected"

    corrections = {c.field: c for c in outcome.corrections}
    assert D(corrections["cash_delta"].broker_value) == CORRECTED_CASH_DELTA
    assert D(corrections["fees"].broker_value) == CORRECTED_FEES
    assert corrections["cash_delta"].scope == "own"
    assert corrections["fees"].scope == "own"


def test_newest_own_scoped_correction_wins_after_retraction_and_fresh_reconcile():
    """The full incident lifecycle: a polluted own-scoped correction already
    exists (43.68/6.32, appended by the pre-retraction reconcile), then the
    id is retracted and a FRESH reconcile appends new own-scoped corrections
    (34.40/5.60). Both records remain in the append-only log, but
    `corrected_value` must render the NEWEST -- the corrected one -- for
    each field."""
    events = _backfilled_log()
    broker = _SharedAccountBroker(fills=_own_rows(), settlements=[])

    # First reconcile: polluted 43.68/6.32, own-scoped.
    first = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-12T09:05:00-04:00")
    asyncio.run(first.reconcile_day(DAY))

    pre_retraction_records = [e for e in events if isinstance(e, CorrectionRecord)]
    assert len(pre_retraction_records) == 2  # cash_delta, fees
    assert corrected_value(events, DAY, "cash_delta", D("40.00")) == TRUE_CASH_DELTA
    assert corrected_value(events, DAY, "fees", D("0.00")) == TRUE_FEES

    # Retract the operator's mistakenly-claimed order id.
    retract_own_order_ids(
        events, ENTRY_ID, [(LEX_ORDER_ID, "operator's own out-of-band order, not the bot's")],
        at="2026-07-14T09:00:00-04:00", note="operator ruling 2026-07-14, strict OWN-01")

    # Fresh reconcile: appends NEW own-scoped records with the corrected values.
    second = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-14T09:10:00-04:00")
    asyncio.run(second.reconcile_day(DAY))

    all_records = [e for e in events if isinstance(e, CorrectionRecord)]
    assert len(all_records) == 4  # the 2 old (still in the log) + 2 new

    # The OLD polluted records are still there, unchanged (append-only).
    old_cash = next(e for e in all_records if e.field == "cash_delta" and e.broker_value == "43.68")
    old_fees = next(e for e in all_records if e.field == "fees" and e.broker_value == "6.32")
    assert old_cash.scope == "own" and old_fees.scope == "own"

    # But the rendered value is the NEWEST own-scoped record for each field.
    assert corrected_value(events, DAY, "cash_delta", D("40.00")) == CORRECTED_CASH_DELTA
    assert corrected_value(events, DAY, "fees", D("0.00")) == CORRECTED_FEES
