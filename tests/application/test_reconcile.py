"""Reconcile — REC-02..05 unit + prose TCs (TC-REC-02/03/04)."""
import asyncio

from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import LongSaleStarted, ReconciliationMismatch, StopReplaced
from tests.harness.fake_broker import FakeBroker


def _rec():
    return Reconcile(FakeBroker(), [])


def test_tc_rec_02_divergence_logs_mismatch_and_gates():
    """TC-REC-02: broker vs internal disagreement -> ReconciliationMismatch
    logged (RSK-03 gate applies)."""
    broker, events = FakeBroker(), []
    rec = Reconcile(broker, events)
    plan = rec.plan(tracked_shorts=[], broker_working_order_ids=set(),
                    mid_lex_sides=[], stale_entry_order_ids=[],
                    position_mismatches=["SPXW_5990P: broker -3 vs ledger -2"])
    asyncio.run(rec.execute(plan))
    assert any(isinstance(e, ReconciliationMismatch) for e in events)


def test_tc_rec_03_reattaches_confirmed_stops_no_reorder():
    """TC-REC-03: a short whose stop is still working is re-attached, not
    re-placed."""
    broker, events = FakeBroker(), []
    stop_id = asyncio.run(broker.submit({"type": "stop_market", "leg": "short_put", "entry_id": "e1"}))
    rec = Reconcile(broker, events)
    plan = rec.plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id=stop_id, stop_filled=False)],
        broker_working_order_ids={stop_id}, mid_lex_sides=[], stale_entry_order_ids=[])
    asyncio.run(rec.execute(plan))
    assert plan.place_stops == []
    assert not any(isinstance(e, StopReplaced) for e in events)


def test_tc_rec_04_1_filled_stop_runs_lex():
    """TC-REC-04(1): a short with no working stop whose stop FILLED -> LEX."""
    rec = _rec()
    plan = rec.plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id="x", stop_filled=True)],
        broker_working_order_ids=set(), mid_lex_sides=[], stale_entry_order_ids=[])
    assert plan.run_lex == [("e1", "PUT")] and plan.place_stops == []


def test_tc_rec_04_2_operator_cancelled_is_user_unprotected():
    """TC-REC-04(2): stop cancelled by operator -> stand down, do NOT re-place."""
    rec = _rec()
    plan = rec.plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id="x",
                                     stop_filled=False, stop_cancelled_by_operator=True)],
        broker_working_order_ids=set(), mid_lex_sides=[], stale_entry_order_ids=[])
    assert plan.user_unprotected == [("e1", "PUT")] and plan.place_stops == []


def test_tc_rec_04_3_genuinely_unprotected_replaces_stop():
    """TC-REC-04(3): short with no stop, not filled, not operator-cancelled ->
    UNPROTECTED re-place."""
    broker, events = FakeBroker(), []
    rec = Reconcile(broker, events)
    plan = rec.plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id=None, stop_filled=False)],
        broker_working_order_ids=set(), mid_lex_sides=[], stale_entry_order_ids=[])
    asyncio.run(rec.execute(plan))
    assert plan.place_stops == [("e1", "PUT")]
    assert any(isinstance(e, StopReplaced) for e in events)


def test_rec_05_recovery_never_duplicates_a_stop():
    """REC-05: idempotency — the same unprotected side listed twice places one stop."""
    broker, events = FakeBroker(), []
    rec = Reconcile(broker, events)
    plan = rec.plan(
        tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id=None, stop_filled=False),
                        TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id=None, stop_filled=False)],
        broker_working_order_ids=set(), mid_lex_sides=[], stale_entry_order_ids=[])
    asyncio.run(rec.execute(plan))
    assert sum(isinstance(e, StopReplaced) for e in events) == 1
