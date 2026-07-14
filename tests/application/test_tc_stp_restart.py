"""TC-STP-05 (EC-STP-02) and TC-STP-11 (EC-STP-06): crash/restart drills against
the reconcile pass. On restart the bot rebuilds intent from the log, reads
broker truth, and REC-04 either re-places genuinely missing stops (idempotently)
or synthesizes the stop-out that happened while it was down and starts LEX."""
import asyncio
from decimal import Decimal as D

from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import LongSaleStarted, ShortStopped, StopReplaced


class RecordingBroker:
    def __init__(self):
        self.submitted = []      # idempotency keys, in submit order
        self.cancelled = []

    async def submit(self, order):
        self.submitted.append(order.idempotency_key)
        return f"ord-{len(self.submitted)}"

    async def cancel(self, id):
        self.cancelled.append(id)
        return {"result": "cancelled"}


def test_tc_stp_05_crash_between_fill_and_stop_places_stops_idempotently():
    """TC-STP-05 (EC-STP-02): a crash between fill and stop placement ⇒ on
    restart REC-04 places the missing stops; a stop that HAD been accepted is
    re-attached, not duplicated."""
    events: list = []
    rec = Reconcile(RecordingBroker(), events)

    # neither short has a resting stop and neither filled -> place both (REC-04(3))
    plan = rec.plan(
        tracked_shorts=[
            TrackedShort("e1", "PUT", "SPXW_P", stop_order_id=None, stop_filled=False, stop_trigger=D("3.80")),
            TrackedShort("e1", "CALL", "SPXW_C", stop_order_id=None, stop_filled=False, stop_trigger=D("3.80")),
        ],
        broker_working_order_ids=set(),
        mid_lex_sides=[], stale_entry_order_ids=[],
    )
    assert set(plan.place_stops) == {("e1", "PUT"), ("e1", "CALL")}

    broker = RecordingBroker()
    asyncio.run(Reconcile(broker, events).execute(plan))
    assert broker.submitted == ["stop:e1:PUT", "stop:e1:CALL"]  # keyed, one each
    assert sum(isinstance(e, StopReplaced) for e in events) == 2

    # idempotency: the PUT stop had actually been accepted (it IS working) ⇒
    # re-attach, place only the genuinely missing CALL — no duplicate PUT stop.
    events2: list = []
    plan2 = Reconcile(RecordingBroker(), events2).plan(
        tracked_shorts=[
            TrackedShort("e1", "PUT", "SPXW_P", stop_order_id="stopPUT", stop_filled=False),
            TrackedShort("e1", "CALL", "SPXW_C", stop_order_id=None, stop_filled=False, stop_trigger=D("3.80")),
        ],
        broker_working_order_ids={"stopPUT"},  # broker already holds the PUT stop
        mid_lex_sides=[], stale_entry_order_ids=[],
    )
    assert plan2.place_stops == [("e1", "CALL")]  # PUT re-attached, not re-placed
    broker2 = RecordingBroker()
    asyncio.run(Reconcile(broker2, events2).execute(plan2))
    assert broker2.submitted == ["stop:e1:CALL"]  # exactly one, no duplicate PUT


def test_tc_stp_11_stop_filled_while_down_synthesizes_event_and_starts_lex():
    """TC-STP-11 (EC-STP-06): a stop that filled while the bot was down ⇒ on
    restart the missed ShortStopped is synthesized (with slippage from the
    recorded trigger) and LEX begins for that side."""
    events: list = []
    plan = Reconcile(RecordingBroker(), events).plan(
        tracked_shorts=[
            TrackedShort("e1", "PUT", "SPXW_P", stop_order_id="s1", stop_filled=True,
                         stop_fill_price=D("3.90"), stop_trigger=D("3.80")),
        ],
        broker_working_order_ids=set(),
        mid_lex_sides=[], stale_entry_order_ids=[],
    )
    assert plan.synthesize_stopped == [("e1", "PUT", D("3.90"), D("0.10"), 1)]
    assert plan.run_lex == [("e1", "PUT")]

    asyncio.run(Reconcile(RecordingBroker(), events).execute(plan))

    # the log reads: synthesized stop-out THEN LEX begins, in that order
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ShortStopped", "LongSaleStarted"]
    stopped = next(e for e in events if isinstance(e, ShortStopped))
    assert stopped.fill == D("3.90") and stopped.slippage == D("0.10")
    assert stopped.initiator == "resting_stop"
    assert any(isinstance(e, LongSaleStarted) for e in events)
    # PNL-01: a synthesized stop-out is still a CLOSE (commission-free), but
    # clearing/ORF/exchange still apply. Per-share: real $0.72 / 100.
    assert stopped.fee == D("0.0072")


def test_tc_stp_11_synthesized_fee_is_contracts_invariant():
    """PNL-01: the fee is PER-SHARE (domain/fees.py) -- `reporting/folds.py`'s
    `entry_dollars` rescales by the entry's contracts exactly ONCE, at the
    reporting layer. A 3-contract short's synthesized stop-out carries the
    SAME per-share fee as a 1-contract one; TrackedShort.contracts sizes the
    re-placed stop (ENT-04), not a second, double-counting fee multiplication."""
    events: list = []
    plan = Reconcile(RecordingBroker(), events).plan(
        tracked_shorts=[
            TrackedShort("e1", "PUT", "SPXW_P", stop_order_id="s1", stop_filled=True,
                         stop_fill_price=D("3.90"), stop_trigger=D("3.80"), contracts=3),
        ],
        broker_working_order_ids=set(),
        mid_lex_sides=[], stale_entry_order_ids=[],
    )
    assert plan.synthesize_stopped == [("e1", "PUT", D("3.90"), D("0.10"), 3)]
    asyncio.run(Reconcile(RecordingBroker(), events).execute(plan))
    stopped = next(e for e in events if isinstance(e, ShortStopped))
    assert stopped.fee == D("0.0072")
